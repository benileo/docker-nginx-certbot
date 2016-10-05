#!/usr/bin/python

import atexit
import os
import logging
import subprocess
import sys

logging.basicConfig(level=logging.INFO)

server_conf = """
server {{
    # Based on https://mozilla.github.io/server-side-tls/ssl-config-generator/
    # for Nginx 1.11.3
    
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {0};

    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;
    ssl_session_tickets off;
    ssl_certificate {1};
    ssl_certificate_key {2};
    ssl_trusted_certificate {3};

    # intermediate configuration. tweak to your needs.
    ssl_protocols TLSv1 TLSv1.1 TLSv1.2;
    ssl_ciphers 'ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-SHA256:ECDHE-RSA-AES128-SHA256:ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES256-SHA384:ECDHE-RSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA:DHE-RSA-AES128-SHA256:DHE-RSA-AES128-SHA:DHE-RSA-AES256-SHA256:DHE-RSA-AES256-SHA:ECDHE-ECDSA-DES-CBC3-SHA:ECDHE-RSA-DES-CBC3-SHA:EDH-RSA-DES-CBC3-SHA:AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA256:AES256-SHA256:AES128-SHA:AES256-SHA:DES-CBC3-SHA:!DSS';
    ssl_prefer_server_ciphers on;

    # fetch OCSP records from URL in ssl_certificate and cache them
    ssl_stapling on;
    ssl_stapling_verify on;

    # Diffie-Hellman parameter for DHE ciphersuites, recommended 2048 bits
    ssl_dhparam /etc/ssl/certs/dhparam.pem;

    # HSTS (ngx_http_headers_module is required) (15768000 seconds = 6 months)
    add_header Strict-Transport-Security max-age=15768000;

    # A resolver must be set for resolving OCSP responder hostname
    resolver 8.8.4.4 8.8.8.8;

    # If there is a conf file, these directives will
    # be added into the server block. If there is
    # no include directive here then you need to 
    # create a conf file mounted in a volume in
    # /etc/nginx/conf.d/custom/
    # 
    # Note: adding duplicates to the directives defined 
    # above will cause errors.
    {4}
}}
"""

redirect_conf = """
server {{
    listen 80 default_server;
    listen [::]:80 default_server;
    return 301 https://{0}$request_uri;
}}
"""


class Certbot(object):
    @classmethod
    def create(cls):
        domain = os.getenv("DOMAIN")
        if not domain:
            fail_with_error_message(
                "DOMAIN must be passed as an env variable")

        email = os.getenv("EMAIL")
        if not email:
            fail_with_error_message(
                "EMAIL must be passed as an env variable")

        certbot = Certbot(domain, email)

        if os.getenv("STAGING"):
            certbot.add_arg("--staging")

        if os.getenv("SERVER"):
            certbot.add_arg("--server", os.getenv("SERVER"))

        if os.getenv("DEBUG"):
            certbot.add_arg("-vvv", "--text")

        return certbot

    def __init__(self, domain, email):
        self.domain = domain
        self.email = email

        self.args = dict()
        self.cmd = ["certbot", "certonly"]

        self.add_arg("--standalone")
        self.add_arg("--agree-tos")
        self.add_arg("--must-staple")
        self.add_arg("--non-interactive")
        self.add_arg("--rsa-key-size", "4096")
        self.add_arg("--standalone-supported-challenges", "http-01")
        self.add_arg("--post-hook", "post-hook.sh")
        self.add_arg("--domain", self.domain)
        self.add_arg("--email", self.email)

    def add_arg(self, key, value=None):
        self.args[key] = value

    def _should_run(self):
        return not os.path.exists(self.live_dir_path("fullchain.pem"))

    def live_dir_path(self, arg):
        bdir = "/etc/letsencrypt/live"
        return os.path.join(bdir, self.domain, arg)

    def fullchain(self):
        return self.live_dir_path("fullchain.pem")

    def privkey(self):
        return self.live_dir_path("privkey.pem")

    def chain(self):
        return self.live_dir_path("chain.pem")

    def run(self):
        if not self._should_run():
            logging.info("certificates exist, not installing")
            return

        for k, v in self.args.iteritems():
            self.cmd.append(k)
            if v is not None:
                self.cmd.append(v)

        logging.info(
            "obtaining certificates for {}".format(self.domain))

        try:
            subprocess.check_call(self.cmd)
        except subprocess.CalledProcessError as err:
            fail_with_error_message(
                "Command failed: {}".format(" ".join(self.cmd)))


def create_conf(certbot):
    create_redirect(certbot.domain)

    custom_include = ""
    if os.path.exists("/etc/nginx/conf.d/custom/"):
        logging.info("Including custom configuration")
        custom_include = "include /etc/nginx/conf.d/custom/*.conf;"

    fp = os.path.join("/etc/nginx/conf.d", certbot.domain + ".conf")
    with open(fp, "w") as fd:
        fd.write(server_conf.format(
            certbot.domain,
            certbot.fullchain(),
            certbot.privkey(),
            certbot.chain(),
            custom_include))

    logging.info(
        "virtual host created for {}".format(certbot.domain))


def fail_with_error_message(msg):
    logging.error(msg)
    sys.exit(1)


def create_redirect(domain):
    with open("/etc/nginx/conf.d/redirect.conf", "w") as fd:
        fd.write(redirect_conf.format(domain))
    logging.info("http redirect created")


processes = []


def shutdown():
    logging.info("performing shutdown clean up")
    for p in processes:
        if p.poll() is None:
            logging.info("sending SIGTERM to pid {}".format(p.pid))
            p.terminate()


def main():
    # make sure to kill started processes upon exit
    atexit.register(shutdown)

    processes.append(subprocess.Popen(["rsyslogd", "-n"]))
    logging.info("starting rsyslogd ({})".format(processes[0].pid))

    processes.append(subprocess.Popen(["cron", "-f"]))
    logging.info("starting cron ({})".format(processes[1].pid))

    # run certbot to get the tls certificate if not existing
    certbot = Certbot.create()
    certbot.run()

    # create a nginx virtual host configuration file
    create_conf(certbot)

    # start nginx in the foreground
    logging.info("starting nginx")
    subprocess.check_call(["nginx", "-g", "daemon off;"])


if __name__ == "__main__":
    main()
