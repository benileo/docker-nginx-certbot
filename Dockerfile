FROM nginx:1.11.3

MAINTAINER BenJammin Irving "jammin.irving@gmail.com"

RUN openssl dhparam -out /etc/ssl/certs/dhparam.pem 2048

RUN apt-get update && apt-get install -y \
		cron \
		rsyslog \
		curl \
		--no-install-recommends

ENV CERTBOT_VERSION 0.9.3
ENV BASE_URL https://github.com/certbot/certbot/archive/v

WORKDIR /opt/certbot

RUN set -e \
    && curl -sL "${BASE_URL}${CERTBOT_VERSION}.tar.gz" | tar xzf - \
    && cd "certbot-${CERTBOT_VERSION}" \
    && ./certbot-auto --os-packages-only --non-interactive \
    && curl -sL https://bootstrap.pypa.io/get-pip.py | python \
    && pip install acme certbot

RUN set -e \
    && apt-get remove -y --purge curl \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf \
        /var/lib/apt/lists/* \
        /tmp/* \
        /var/tmp/*

COPY post-hook.sh /usr/local/bin/post-hook.sh
COPY rsyslog.conf /etc/rsyslog.conf
COPY entrypoint.py .

ENTRYPOINT [ "./entrypoint.py" ]