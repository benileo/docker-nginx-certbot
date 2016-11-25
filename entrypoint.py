#!/usr/bin/python

import atexit
import os
import logging
import signal
import subprocess
import sys
import threading
import time
import Queue

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s:autotls:%(message)s")

# Based on https://mozilla.github.io/server-side-tls/ssl-config-generator/
# for Nginx 1.11.3
TLS_CONFIG = """
server {{
    server_name {0};
    listen 443 ssl http2;
    # listen [::]:443 ssl http2;

    ssl_certificate {1};
    ssl_certificate_key {2};
    ssl_ciphers 'ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-SHA256:ECDHE-RSA-AES128-SHA256:ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES256-SHA384:ECDHE-RSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA:DHE-RSA-AES128-SHA256:DHE-RSA-AES128-SHA:DHE-RSA-AES256-SHA256:DHE-RSA-AES256-SHA:ECDHE-ECDSA-DES-CBC3-SHA:ECDHE-RSA-DES-CBC3-SHA:EDH-RSA-DES-CBC3-SHA:AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA256:AES256-SHA256:AES128-SHA:AES256-SHA:DES-CBC3-SHA:!DSS';
    ssl_dhparam /etc/ssl/certs/dhparam.pem;
    ssl_prefer_server_ciphers on;
    ssl_protocols TLSv1 TLSv1.1 TLSv1.2;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;
    ssl_session_tickets off;
    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_trusted_certificate {3};

    # HSTS (ngx_http_headers_module is required) (15768000 seconds = 6 months)
    add_header Strict-Transport-Security max-age=15768000;

    # A resolver must be set for resolving OCSP responder hostname
    # This should be configurable
    resolver 8.8.4.4 8.8.8.8;

    # If there is a conf file, these directives will
    # be added into the server block. If there is
    # no include directive here then you need to
    # create a conf file mounted in a volume in
    # /etc/nginx/conf.d/custom/
    #
    # Note: adding duplicates to the directives defined
    # above will cause errors. This sucks....
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

NGINX_CMD = ["/usr/sbin/nginx", "-g", "daemon off;"]

NGINX_RENEW_CMD = ("""certbot renew --pre-hook 'nginx -s stop; sleep 2' """
                   """--preferred-challenges http-01 --agree-tos """
                   """--standalone --must-staple """)

NGINX_FORCE_RENEW_CMD = NGINX_RENEW_CMD + "--force-renewal"


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
    done_lock = threading.Lock()

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
        # if self.config.get("debug"):
        #     self.add_arg("-vvv", "--text")
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
    _handle = None
    _running = False
    _exiting = False
    config_path = "/etc/nginx/conf.d/reverse_proxy.conf"
    _lock = threading.Lock()

    @classmethod
    def disallow_start(cls):
        cls._lock.acquire()

    @classmethod
    def allow_start(cls):
        cls._lock.release()

    @classmethod
    def is_running(cls):
        return cls._running

    @classmethod
    def run_forever(cls):
        while 1:
            if cls._handle is None:
                cls._start()
            try:
                cls._handle.wait()
            except KeyboardInterrupt:
                cls.stop()
                logging.info("goodbye")
                break
            cls._running = False
            cls._handle = None
            logging.debug('nginx process has stopped')
            if cls._exiting:
                break

    @classmethod
    def _start(cls):
        cls._lock.acquire()
        logging.info('starting nginx')
        cls._handle = subprocess.Popen(NGINX_CMD)
        cls._running = True
        cls._lock.release()

    @classmethod
    def reload(cls):
        logging.info("reloading nginx")
        cls._handle.send_signal(signal.SIGHUP)

    @classmethod
    def stop(cls):
        logging.info("stopping nginx")
        if cls._handle and cls._handle.poll() is None:
            cls._handle.send_signal(signal.SIGQUIT)
        cls._handle = None

    @classmethod
    def exit(cls):
        logging.debug("calling on exit callback")
        cls._exiting = True
        if cls._handle:
            cls.stop()

    @classmethod
    def remove_proxy_config(cls):
        if os.path.exists(cls.config_path):
            os.remove(cls.config_path)

    @classmethod
    def write_proxy_config(cls):
        with open(cls.config_path, "w") as fo:
            fo.write(PROXY_CONFIG)


def certs_exist(domain):
    """
    The existence of the full chain certificate is the indication of whether
    we try to obtain certificates for Let's Encrypt.

    :param domain string
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
    :rtype Config
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


def wait_for_nginx():
    while not Nginx.is_running():
        pass


def obtain_cert(config):
    """
    Go and get the certificates for LE
    :param config Config
    :return:
    """
    certbot = Certbot(config)
    wait_for_nginx()

    Nginx.write_proxy_config()
    certbot.run()
    Nginx.remove_proxy_config()

    create_nginx_config_file(certbot.domain)
    Nginx.reload()

    # give nginx a second to reload, todo: use something proper
    time.sleep(1)
    # signal to the renewer that it can start
    Certbot.done_lock.release()


def run_renewer(config, queue):
    """
    Nginx must be running
    And certbot cmd must be done by now

    Note: this may be an idea for certbot: I don't think we actually
    have to stop the nginx server here because we don't need to
    re-validate our key. We have a validated key at this point. We
    just need to hit boulders `NewCertificate` endpoint and then
    reload nginx instead of stopping it. My only concern with this is
    permissions regarding over-writing the certificates. If they are
    currently being held in memory by the Nginx master process then
    will we be allowed.

    :type queue: Queue.Queue
    :type config: Config
    """
    wait_for_nginx()
    Certbot.done_lock.acquire()
    logging.info('starting renewer')
    while 1:
        # try and renew right away - to see if anything will go wrong.
        Nginx.disallow_start()  # Nginx is locked and won't restart
        try:
            if config.debug:
                subprocess.check_call(NGINX_FORCE_RENEW_CMD, shell=True)
            else:
                subprocess.check_call(NGINX_RENEW_CMD, shell=True)
        except subprocess.CalledProcessError as perr:
            logging.debug("renewer: error renewing certificate: %s", perr)
        finally:
            Nginx.allow_start()
            for i in range(1800):
                try:
                    queue.get(block=True, timeout=2)
                except Queue.Empty:
                    pass
                else:
                    # thread exits cleanly upon receiving an item for the queue
                    logging.debug('renewer: whoa! it\'s time to stop')
                    return


def sigterm_handler():
    logging.debug('agressive! SIGTERM received')
    sys.exit(0)


def main():
    config = parse_environment()
    domain = config.get("domain")

    # todo: remove this lock
    Certbot.done_lock.acquire()

    exiting = Queue.Queue(maxsize=1)
    signal.signal(signal.SIGTERM, sigterm_handler)
    # run renewal loop in a thread
    renewer = threading.Thread(target=run_renewer, args=(config,exiting))
    renewer.start()

    # Case 1: we don't have a certificate yet
    # write the proxy config and then run certbot. note that the thread will
    # wait for nginx to start before it starts up.
    # certbot is only ran under the circumstance that the certificates dont
    # already exist
    if certs_exist(domain):
        threading.Thread(target=obtain_cert, args=(config,)).start()

    # Case 2: we already have a certificate
    # nginx runs forever, what about the case where we have a certificate
    # but its expired... release the certbot done lock because we never actually
    # ran it.
    else:
        logging.info('we already have the existing certificates')
        Certbot.done_lock.release()
        create_nginx_config_file(domain)

    Nginx.run_forever()
    exiting.put(True)
    renewer.join()
    logging.debug('exiting')

if __name__ == "__main__":
    main()
