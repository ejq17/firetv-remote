#!/usr/bin/env python3
"""Fake Fire TV for testing the bridge without a real device.
Speaks the same HTTPS API on --port (default 18080) and DIAL on --dial (default 18009).
PIN is always 1234. Run:  python3 dev/mock_firetv.py
Then point the bridge at it with a config like:
  {"ip": "127.0.0.1", "firetv_port": 18080, "dial_port": 18009}
"""
import argparse, json, os, ssl, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TOKEN = "MOCKTOKEN7"
LOG = []

class API(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0); return self.rfile.read(n).decode() if n else ""
    def _authed(self):
        return self.headers.get("X-Client-Token") == TOKEN
    def do_GET(self):
        u = urlparse(self.path)
        if self.headers.get("X-Api-Key") != "0987654321": return self._send(403, {"description": "bad api key"})
        if u.path == "/v1/FireTV/appsV2":
            if not self._authed(): return self._send(401, {"description": "authn"})
            return self._send(200, [{"packageName": "com.netflix.ninja", "displayName": "Netflix"},
                                    {"packageName": "com.plexapp.android", "displayName": "Plex"}])
        return self._send(404, {"description": "nope"})
    def do_POST(self):
        u = urlparse(self.path); q = parse_qs(u.query); body = self._body()
        print("MOCK <-", self.command, self.path, body, "token=" + str(self.headers.get("X-Client-Token")),
              "ctype=" + str(self.headers.get("Content-Type")), flush=True)
        if self.headers.get("X-Api-Key") != "0987654321": return self._send(403, {"description": "bad api key"})
        if u.path == "/v1/FireTV/pin/display":
            print("MOCK: TV SCREEN SHOWS PIN 1234 for", json.loads(body).get("friendlyName"), flush=True)
            return self._send(200, {"description": "OK"})
        if u.path == "/v1/FireTV/pin/verify":
            if json.loads(body).get("pin") == "1234": return self._send(200, {"description": TOKEN})
            return self._send(401, {"description": "wrong pin"})
        if not self._authed(): return self._send(401, {"description": "authn"})
        if u.path == "/v1/FireTV" and q.get("action"):
            return self._send(200, {"description": "OK"})
        if u.path == "/v1/media" and q.get("action"):
            if q["action"][0] == "scan":
                try: d = json.loads(body)
                except ValueError: d = {}
                if d.get("direction") not in ("forward", "back") or "charset" not in (self.headers.get("Content-Type") or ""):
                    return self._send(400, {"description": "Bad arguments supplied. Please check inputs."})
            return self._send(200, {"description": "OK"})
        if u.path.startswith("/v1/FireTV/app/"): return self._send(200, {"description": "OK"})
        if u.path == "/v1/FireTV/keyboard": return self._send(200, {"description": "OK"})
        return self._send(404, {"description": "nope"})

class DIAL(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        print("MOCK DIAL <- POST", self.path, flush=True)
        self.send_response(201 if self.path == "/apps/FireTVRemote" else 404); self.send_header("Content-Length", "0"); self.end_headers()
    def do_GET(self):
        if self.path == "/apps/FireTVRemote":
            b = b"<service><name>FireTVRemote</name><state>running</state></service>"
            self.send_response(200); self.send_header("Content-Type", "text/xml"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=18080); ap.add_argument("--dial", type=int, default=18009)
    a = ap.parse_args()
    d = tempfile.mkdtemp(); crt, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "2", "-subj", "/CN=mock-firetv"], check=True, capture_output=True)
    api = ThreadingHTTPServer(("127.0.0.1", a.port), API)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(crt, key)
    api.socket = ctx.wrap_socket(api.socket, server_side=True)
    dial = ThreadingHTTPServer(("127.0.0.1", a.dial), DIAL)
    threading.Thread(target=dial.serve_forever, daemon=True).start()
    print("Mock Fire TV: https://127.0.0.1:%d  DIAL http://127.0.0.1:%d  PIN=1234" % (a.port, a.dial), flush=True)
    api.serve_forever()

if __name__ == "__main__": main()
