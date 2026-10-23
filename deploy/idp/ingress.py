"""Local HTTP relay: no GPU, models, credentials, document parsing or free URLs.

Only this small process joins the ingress network. Both model interpreters remain
on an internal Docker network without a default route. Bodies stream in 64KiB
chunks to one fixed upstream; request count, bytes and response bytes are bounded.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SLOTS = threading.BoundedSemaphore(16)
MAX_REQUEST = 48 * 1024**2
MAX_RESPONSE = 16 * 1024**2


class Relay(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def error(self, code):
        raw = json.dumps({"error": "LOCAL_INFERENCE_RELAY_UNAVAILABLE"}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def forward(self):
        allowed = self.command == "GET" and (self.path == "/health" or re.fullmatch(r"/status/[a-f0-9]{32}", self.path))
        allowed = allowed or self.command == "POST" and self.path in {"/extract", "/refine", "/interpret", "/cancel"}
        if not allowed:
            self.close_connection = True
            self.error(404)
            return
        if not SLOTS.acquire(blocking=False):
            self.close_connection = True
            self.error(503)
            return
        upstream = http.client.HTTPConnection("idp-gateway", 8088, timeout=1800)
        sent = False
        try:
            length = int(self.headers.get("Content-Length", 0))
            limit = 1024 if self.path == "/cancel" else MAX_REQUEST
            if not 0 <= length <= limit or self.command == "GET" and length:
                raise ValueError("REQUEST_LIMIT")
            upstream.putrequest(self.command, self.path)
            upstream.putheader("Content-Type", "application/json")
            upstream.putheader("Content-Length", str(length))
            upstream.endheaders()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ValueError("REQUEST_INCOMPLETE")
                upstream.send(chunk)
                remaining -= len(chunk)
            response = upstream.getresponse()
            result_size = int(response.getheader("Content-Length", "0"))
            if not 0 < result_size <= MAX_RESPONSE:
                raise ValueError("RESPONSE_LIMIT")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(result_size))
            self.end_headers()
            sent = True
            remaining = result_size
            while remaining:
                chunk = response.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ValueError("RESPONSE_INCOMPLETE")
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (OSError, ValueError, http.client.HTTPException):
            self.close_connection = True
            if not sent:
                self.error(503)
        finally:
            upstream.close()
            SLOTS.release()

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8088), Relay).serve_forever()
