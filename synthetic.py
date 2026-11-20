"""Synthetic loopback-only HTTP fixtures; never use as an ingress."""
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread


@contextmanager
def synthetic_server(respond):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch(None)

        def do_POST(self):
            self.connection.settimeout(2)
            self.dispatch(json.loads(self.rfile.read(int(self.headers['Content-Length']))))

        def dispatch(self, body):
            calls.append((self.path, self.headers, body))
            reply = respond(self.path, body, self.headers)
            if reply is None:  # Fixture recorded the request but drops the response.
                self.close_connection = True
                return
            status, headers, raw = reply
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass
    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        worker.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}', calls
        finally:
            server.shutdown()
            worker.join(2)


def json_response(data):
    return 200, {'Content-Type': 'application/json'}, json.dumps(data).encode()
