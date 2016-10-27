# Nginx AutoTLS


A docker container that automates the creation of TLS certificates using certbot and uses a secure nginx configuration. This is current a work in progress.

## Development
For testing against a local boulder you need to make sure that you change the boulder validation authority configuration to the following in `test/config/va.json`

       "portConfig": {
           "httpPort": 80,
           "httpsPort": 443,
           "tlsPort": 443,
       }

Running boulder:

       docker-compose run -e FAKE_DNS=172.17.0.1 --service-ports boulder ./start.py

Running nginx-autotls:

         docker run -it \
	         --network="host" \
	         -e SERVER=http://127.0.0.1:4000/directory \
	         -e DOMAIN=le.wtf \
	         -e EMAIL=email@mailinator.com \
	         $(docker build -q ./)
