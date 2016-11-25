#!/usr/bin/env python

import atexit
import BaseHTTPServer
import SocketServer
server = None
port = 5002
host = '0.0.0.0'


def shutdown():
    if server is not None:
        server.server_close()
atexit.register(shutdown)


class RedirectHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def do_GET(self):
        print 'incoming...'
        rhost = self.headers.getheader('Host')
        rhost = rhost.split(':')[0]
        location = 'https://{0}{1}'.format(rhost, self.path)
        print 'redirecting to ' + location
        self.send_response(301)
        self.send_header('Location', location)
        self.end_headers()

if __name__ == "__main__":
    server = SocketServer.TCPServer((host, port), RedirectHandler)
    server.allow_reuse_address = True
    print 'starting http -> https redirect server'
    server.serve_forever()
    server.server_close()
