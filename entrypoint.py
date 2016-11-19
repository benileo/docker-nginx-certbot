#!/usr/bin/python

import atexit
import os
import logging
import signal
import subprocess
import sys
import threading

logging.basicConfig(level=logging.INFO)

TLS_CONFIG = """
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

PROXY_CONFIG = """
server {
    listen 443 ssl;
    server_name _;
    ssl_certificate /etc/nginx/ssl/nginx.crt;
    ssl_certificate_key /etc/nginx/ssl/nginx.key;
    location / {
        proxy_pass http://localhost;
    }
}
"""

LE_BASE_DIR = "/etc/letsencrypt/live"

FULL_CHAIN = "fullchain.pem"

PRIVATE_KEY = "privkey.pem"

CHAIN = "chain.pem"

NGINX_CMD = ["nginx", "-g", "daemon off;"]


class Config(object):

    defaults = {
        'debug': False,
        'domain': None,
        'email': None,
        'server': None,
        'staging': False,
    }

    def __init__(self):
        for k, v in Config.defaults.iteritems():
            setattr(self, k, v)

    def set(self, key, value):
        setattr(self, key, value)

    def get(self, key, default=None):
        if hasattr(self, key):
            return self.__getattribute__(key)
        return default


class Certbot(object):

    def __init__(self, config):
        self.args = dict()
        self.config = config
        self.domain = self.config.get("domain")
        self.email = self.config.get("email")
        self.cmd = ["certbot", "certonly"]

        self.add_arg("--domain", self.domain)
        self.add_arg("--email", self.email)
        self.add_arg("--standalone")
        self.add_arg("--non-interactive")
        self.add_arg("--agree-tos")
        self.add_arg("--must-staple")

        if self.config.get("staging"):
            self.add_arg("--staging")
        if self.config.get("debug"):
            self.add_arg("-vvv", "--text")
        if self.config.get("server"):
            self.add_arg("--server", self.config.get("server"))

        # We must use HTTP-01 - as we will be using TLS-SNI raw packet routing
        # in front of this and TLS-SNI based challenges use the reserved name
        # acme.invalid
        self.add_arg("--preferred-challenges", "http-01")

    def add_arg(self, key, value=None):
        self.args[key] = value

    def run(self):
        """
        Build the certbot command array using self.args
        :return: None
        """
        for k, v in self.args.iteritems():
            self.cmd.append(k)
            if v is not None:
                self.cmd.append(v)

        logging.info("obtaining certificates for {}".format(self.domain))
        try:
            subprocess.check_call(self.cmd)
        except subprocess.CalledProcessError:
            # ouch, these should be handled better.
            fail_with_error_message(
                "Command failed: {}".format(" ".join(self.cmd)))


class Nginx(object):
    handle = None
    config_path = "/etc/nginx/conf.d/reverse_proxy.conf"
    start_lock = threading.Lock()

    @classmethod
    def start(cls):
        if cls.handle is None:
            cls.handle = Process.start(NGINX_CMD)
            cls.start_lock.release()
            cls.handle.wait()  # wait forever

    @classmethod
    def write_proxy_config(cls):
        with open(cls.config_path, "w") as fo:
            fo.write(PROXY_CONFIG)

    @classmethod
    def remove_proxy_config(cls):
        if os.path.exists(cls.config_path):
            os.remove(cls.config_path)

    @classmethod
    def reload(cls):
        logging.info("reloading nginx")
        cls.handle.send_signal(signal.SIGHUP)


class Process(object):
    processes = []

    @classmethod
    def start(cls, cmd, add=True):
        if not isinstance(cmd, list):
            raise ValueError('arguments must be of type list')
        handle = subprocess.Popen(cmd)
        logging.info("started {} ({})".format(cmd[0], handle.pid))
        cls.processes.append(handle)

        return handle

    @classmethod
    def kill(cls, handle):
        if not isinstance(handle, subprocess.Popen):
            raise ValueError('argument must of type Popen')
        logging.info("sending SIGKILL to pid {}".format(handle.pid))
        handle.kill()

    @classmethod
    def kill_all(cls):
        logging.info("performing shutdown clean up")
        for process in cls.processes:
            if process.poll() is None:
                cls.kill(process)


def should_obtain_certificates(domain):
    """
    The existence of the full chain certificate is the indication of whether
    we try to obtain certificates for Let's Encrypt.

    :param domain:
    :return: bool
    """
    return not os.path.exists(live_dir_path(domain, FULL_CHAIN))


def live_dir_path(domain, path):
    return os.path.join(LE_BASE_DIR, domain, path)


def create_nginx_config_file(domain):
    """
    TODO: make this nicer, oh my goodness!

    :idea use an nginx parser - or have a different way of creating the nginx
    config, this is very inflexible. would the cerbot nginx plugin be useful?
    :param domain:
    :return:
    """
    custom_include = ""
    if os.path.exists("/etc/nginx/conf.d/custom/"):
        logging.info("Including custom configuration")
        custom_include = "include /etc/nginx/conf.d/custom/*.conf;"

    fp = os.path.join("/etc/nginx/conf.d", domain + ".conf")
    with open(fp, "w") as fd:
        fd.write(TLS_CONFIG.format(
            domain,
            live_dir_path(domain, FULL_CHAIN),
            live_dir_path(domain, PRIVATE_KEY),
            live_dir_path(domain, CHAIN),
            custom_include))
    logging.info(
        "virtual host created for {}".format(domain))


def fail_with_error_message(msg):
    logging.error(msg)
    sys.exit(1)


def parse_environment():
    """
    We should be loading the config into an object that represent the environ

    Maybe we piggy back on OS.environ (since this is the way they arrive)

    Or what about if the container was ran like docker run domain email
    we only really care if the domain and email exists
    :return:
    """
    config = Config()

    if not os.getenv("DOMAIN"):
        fail_with_error_message("DOMAIN must be passed as an env variable")

    if not os.getenv("EMAIL"):
        fail_with_error_message("EMAIL must be passed as an env variable")

    config.set("email", os.getenv("EMAIL"))
    config.set("domain", os.getenv("DOMAIN"))
    config.set("server", os.getenv("SERVER", Config.defaults.get("server")))
    config.set("staging", os.getenv("STAGING", Config.defaults.get("staging")))
    config.set("debug", os.getenv("DEBUG", Config.defaults.get("debug")))

    return config


def run_certbot(certbot):
    Nginx.start_lock.acquire()
    certbot.run()
    Nginx.remove_proxy_config()
    create_nginx_config_file(certbot.domain)
    Nginx.reload()


def main():
    atexit.register(Process.kill_all)
    config = parse_environment()
    domain = config.get("domain")

    Nginx.start_lock.acquire()
    if should_obtain_certificates(domain):
        Nginx.write_proxy_config()
        threading.Thread(target=run_certbot, args=(Certbot(config),)).start()
    else:
        create_nginx_config_file(domain)

    Nginx.start()

if __name__ == "__main__":
    main()
