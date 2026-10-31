#!/usr/bin/env python3
"""
Fire TV Remote bridge
=====================
A tiny local server that relays commands from the web remote (the page on
GitHub Pages, or this same page served at http://localhost:8765) to your
Fire TV over your home network.

Why is this needed?  The Fire TV speaks HTTPS with a self-signed certificate
and sends no CORS headers, so a browser page cannot talk to it directly.
This script does the talking instead.  It uses only the Python standard
library (3.8+), no installs required.

Run:    python3 bridge.py
Then:   open https://ejq17.github.io/firetv-remote/   (or http://localhost:8765)

Options:
  --port 8765        port for the bridge itself
  --tv 192.168.1.50  set the Fire TV IP up front (the web page can also set it)
  --bind 0.0.0.0     interface to listen on (0.0.0.0 lets your phone use it too)
"""
import argparse
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("FIRETV_REMOTE_CONFIG") or os.path.expanduser("~/.firetv-remote.json")

# Values used by the official Fire TV phone app (community documented).
API_KEY = "0987654321"
USER_AGENT = "okhttp/4.10.0"
FIRETV_PORT = 8080   # HTTPS remote API
DIAL_PORT = 8009     # HTTP, used to wake the remote service

ALLOWED_KEYS = {
    "dpad_up", "dpad_down", "dpad_left", "dpad_right", "select",
    "home", "back", "menu", "sleep", "power", "backspace", "epg",
    "volume_up", "volume_down", "mute",
}
ALLOWED_MEDIA = {"play", "pause", "scan"}

_cfg_lock = threading.Lock()


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("ip", "")
    cfg.setdefault("token", "")
    cfg.setdefault("name", "")
    cfg.setdefault("firetv_port", FIRETV_PORT)
    cfg.setdefault("dial_port", DIAL_PORT)
    cfg.setdefault("allowed_origins", [])
    return cfg


def save_config(cfg):
    with _cfg_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)


SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class TVError(Exception):
    def __init__(self, message, status=502, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class FireTV:
    """Thin client for the Fire TV local remote API."""

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def ip(self):
        return self.cfg.get("ip", "")

    @property
    def token(self):
        return self.cfg.get("token", "")

    def _require_ip(self):
        if not self.ip:
            raise TVError("No Fire TV IP set yet. Use 'Find TVs' or type the IP.", 400)

    def _base(self):
        return "https://%s:%s" % (self.ip, self.cfg.get("firetv_port", FIRETV_PORT))

    def _request(self, method, url, body=None, auth=True, timeout=5,
                 content_type="application/json; charset=utf-8"):
        headers = {
            "X-Api-Key": API_KEY,
            "User-Agent": USER_AGENT,
            "Content-Type": content_type,
        }
        if auth and self.token:
            headers["X-Client-Token"] = self.token
        data = None
        if method in ("POST", "PUT"):
            data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        ctx = SSL_CTX if url.startswith("https") else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise ConnectionError(str(e))

    def wake(self):
        """Ask the TV (via DIAL on port 8009) to start its remote-control service."""
        self._require_ip()
        url = "http://%s:%s/apps/FireTVRemote" % (self.ip, self.cfg.get("dial_port", DIAL_PORT))
        try:
            status, _ = self._request("POST", url, body=None, auth=False, timeout=5,
                                      content_type="text/plain; charset=utf-8")
            return status in (200, 201, 204)
        except ConnectionError:
            return False

    def _call(self, fn):
        """Run fn(); on connection failure wake the TV and retry once."""
        self._require_ip()
        try:
            return fn()
        except ConnectionError:
            self.wake()
            time.sleep(2)
            try:
                return fn()
            except ConnectionError as e:
                raise TVError("Could not reach the Fire TV at %s. Is it on and on the same Wi-Fi?" % self.ip,
                              502, str(e))

    @staticmethod
    def _parse(text):
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {"raw": text}

    def pin_display(self, friendly_name="Web Remote"):
        def go():
            return self._request("POST", self._base() + "/v1/FireTV/pin/display",
                                 {"friendlyName": friendly_name}, auth=False, timeout=8)
        status, text = self._call(go)
        if status != 200:
            raise TVError("TV refused to show a PIN (HTTP %s)." % status, 502, text)
        return self._parse(text)

    def pin_verify(self, pin):
        def go():
            return self._request("POST", self._base() + "/v1/FireTV/pin/verify",
                                 {"pin": pin}, auth=False, timeout=10)
        status, text = self._call(go)
        data = self._parse(text)
        token = data.get("description") if isinstance(data, dict) else None
        if status != 200 or not token or token.upper() in ("OK", "FAILED"):
            raise TVError("PIN was not accepted (HTTP %s). Try pairing again." % status, 400, text)
        self.cfg["token"] = token
        save_config(self.cfg)
        return {"paired": True}

    def _check_auth(self, status, text):
        if status in (401, 403):
            raise TVError("The TV no longer accepts this remote. Pair again.", 401, text)
        if status not in (200, 500):   # older devices return 500 but still act
            raise TVError("TV returned HTTP %s." % status, 502, text)

    def key(self, key, action="tap"):
        if key not in ALLOWED_KEYS:
            raise TVError("Unknown key '%s'." % key, 400)
        if not self.token:
            raise TVError("Not paired yet. Pair with the TV first.", 401)
        url = self._base() + "/v1/FireTV?action=" + key
        body = None
        if action == "down":
            body = {"keyActionType": "keyDown"}
        elif action == "up":
            body = {"keyActionType": "keyUp"}
        elif action != "tap":
            raise TVError("Unknown key action '%s'." % action, 400)
        status, text = self._call(lambda: self._request("POST", url, body))
        self._check_auth(status, text)
        return {"ok": True, "response": self._parse(text)}

    def media(self, action, direction=None):
        if action not in ALLOWED_MEDIA:
            raise TVError("Unknown media action '%s'." % action, 400)
        if not self.token:
            raise TVError("Not paired yet. Pair with the TV first.", 401)
        url = self._base() + "/v1/media?action=" + action
        body = None
        if action == "scan":
            if direction not in ("forward", "back"):
                raise TVError("scan needs direction 'forward' or 'back'.", 400)
            body = {"direction": direction, "keyAction": {"keyActionType": "keyDownUp"}}
        status, text = self._call(lambda: self._request("POST", url, body))
        self._check_auth(status, text)
        return {"ok": True, "response": self._parse(text)}

    def launch_app(self, package):
        if not re.fullmatch(r"[A-Za-z0-9_.]{3,120}", package or ""):
            raise TVError("That does not look like an app package name.", 400)
        if not self.token:
            raise TVError("Not paired yet. Pair with the TV first.", 401)
        url = self._base() + "/v1/FireTV/app/" + package
        status, text = self._call(lambda: self._request("POST", url, None, timeout=10))
        self._check_auth(status, text)
        return {"ok": True, "response": self._parse(text)}

    def apps(self):
        if not self.token:
            raise TVError("Not paired yet. Pair with the TV first.", 401)
        url = self._base() + "/v1/FireTV/appsV2"
        status, text = self._call(lambda: self._request("GET", url, timeout=10))
        self._check_auth(status, text)
        return {"ok": True, "apps": self._parse(text)}

    def send_text(self, text):
        if not isinstance(text, str) or not text:
            raise TVError("No text given.", 400)
        if not self.token:
            raise TVError("Not paired yet. Pair with the TV first.", 401)
        url = self._base() + "/v1/FireTV/keyboard"
        status, body = self._call(lambda: self._request("POST", url, {"text": text[:500]}))
        self._check_auth(status, body)
        return {"ok": True, "response": self._parse(body)}


# ---------------------------------------------------------------- discovery

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # UDP connect sends nothing; it just picks a route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ssdp_search(timeout=2.5):
    """Find DIAL-capable devices (Fire TV answers these) via SSDP multicast."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           "MAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:dial-multiscreen-org:service:dial:1\r\n\r\n").encode()
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode("utf-8", "replace")
            m = re.search(r"(?im)^LOCATION:\s*(\S+)", text)
            found[addr[0]] = m.group(1) if m else None
    except OSError:
        pass
    finally:
        sock.close()
    return found


def describe(ip, location, dial_port):
    """Return (is_firetv, friendly_name, model) for a candidate device."""
    name = model = ""
    if location:
        try:
            req = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=2) as r:
                xml = r.read().decode("utf-8", "replace")
            m = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S)
            name = (m.group(1).strip() if m else "")
            m = re.search(r"<modelName>(.*?)</modelName>", xml, re.S)
            model = (m.group(1).strip() if m else "")
        except Exception:
            pass
    is_firetv = False
    try:
        req = urllib.request.Request("http://%s:%s/apps/FireTVRemote" % (ip, dial_port),
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=2) as r:
            is_firetv = r.status in (200, 201)
    except urllib.error.HTTPError as e:
        is_firetv = e.code in (200, 201)
    except Exception:
        is_firetv = False
    if not is_firetv and re.search(r"fire ?tv|amazon|aft", (name + " " + model), re.I):
        is_firetv = True
    return is_firetv, name, model


def port_sweep(dial_port, timeout=0.4):
    """Fallback: try every host on the local /24 for an open DIAL port."""
    me = local_ip()
    if me.startswith("127."):
        return []
    prefix = me.rsplit(".", 1)[0]

    def probe(i):
        host = "%s.%d" % (prefix, i)
        if host == me:
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, dial_port))
            return host
        except OSError:
            return None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=128) as ex:
        return [h for h in ex.map(probe, range(1, 255)) if h]


def discover(cfg):
    dial_port = cfg.get("dial_port", DIAL_PORT)
    candidates = ssdp_search()
    for host in port_sweep(dial_port):
        candidates.setdefault(host, None)
    results = []
    for ip, location in candidates.items():
        is_firetv, name, model = describe(ip, location, dial_port)
        results.append({"ip": ip, "firetv": is_firetv, "name": name, "model": model})
    results.sort(key=lambda r: (not r["firetv"], r["ip"]))
    return results


# ---------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "FireTVBridge/1.0"
    cfg = None       # set on the class at startup
    tv = None

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # --- CORS -------------------------------------------------------------
    def _origin_allowed(self, origin):
        if not origin:
            return True                          # same-origin, curl, etc.
        try:
            host = (urlparse(origin).hostname or "").lower()
        except ValueError:
            return False
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        if host.endswith(".github.io"):
            return True
        if origin in self.cfg.get("allowed_origins", []):
            return True
        # the bridge's own LAN address (phone opening http://<mac-ip>:8765)
        return host == local_ip()

    def _cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- helpers ----------------------------------------------------------
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 65536:
            raise TVError("Request too large.", 413)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise TVError("Body is not valid JSON.", 400)

    def _serve_file(self, name, ctype):
        path = os.path.join(HERE, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._json(404, {"error": "%s not found next to bridge.py" % name})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _status(self):
        return {
            "bridge": True,
            "ip": self.cfg.get("ip", ""),
            "paired": bool(self.cfg.get("token")),
            "name": self.cfg.get("name", ""),
        }

    # --- routes -----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        origin = self.headers.get("Origin")
        if origin and not self._origin_allowed(origin):
            return self._json(403, {"error": "Origin not allowed."})
        try:
            if path in ("/", "/index.html"):
                return self._serve_file("index.html", "text/html; charset=utf-8")
            if path == "/bridge.py":
                return self._serve_file("bridge.py", "text/x-python; charset=utf-8")
            if path == "/api/status":
                return self._json(200, self._status())
            if path == "/api/apps":
                return self._json(200, self.tv.apps())
            return self._json(404, {"error": "Not found"})
        except TVError as e:
            return self._json(e.status, {"error": str(e), "detail": e.detail})
        except Exception as e:  # keep the bridge alive no matter what
            return self._json(500, {"error": "Bridge error: %s" % e})

    def do_POST(self):
        path = urlparse(self.path).path
        origin = self.headers.get("Origin")
        if origin and not self._origin_allowed(origin):
            return self._json(403, {"error": "Origin not allowed."})
        try:
            body = self._read_json()
            if path == "/api/config":
                ip = str(body.get("ip", "")).strip()
                if ip and not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}|[A-Za-z0-9.-]{1,253}", ip):
                    raise TVError("That is not a valid IP address.", 400)
                if ip != self.cfg.get("ip"):
                    self.cfg["ip"] = ip
                    self.cfg["token"] = ""          # a new TV needs a new pairing
                    self.cfg["name"] = str(body.get("name", ""))[:80]
                    save_config(self.cfg)
                return self._json(200, self._status())
            if path == "/api/discover":
                return self._json(200, {"devices": discover(self.cfg)})
            if path == "/api/wake":
                return self._json(200, {"ok": self.tv.wake()})
            if path == "/api/pair/start":
                self.tv.wake()
                time.sleep(1)
                name = str(body.get("friendlyName") or "Web Remote")[:40]
                return self._json(200, {"ok": True, "response": self.tv.pin_display(name)})
            if path == "/api/pair/verify":
                pin = re.sub(r"\D", "", str(body.get("pin", "")))
                if len(pin) < 4:
                    raise TVError("Enter the 4-digit PIN shown on the TV.", 400)
                return self._json(200, self.tv.pin_verify(pin))
            if path == "/api/unpair":
                self.cfg["token"] = ""
                save_config(self.cfg)
                return self._json(200, self._status())
            if path == "/api/key":
                return self._json(200, self.tv.key(str(body.get("key", "")), str(body.get("action", "tap"))))
            if path == "/api/media":
                return self._json(200, self.tv.media(str(body.get("action", "")), body.get("direction")))
            if path == "/api/app":
                return self._json(200, self.tv.launch_app(str(body.get("package", ""))))
            if path == "/api/text":
                return self._json(200, self.tv.send_text(body.get("text")))
            return self._json(404, {"error": "Not found"})
        except TVError as e:
            return self._json(e.status, {"error": str(e), "detail": e.detail})
        except Exception as e:
            return self._json(500, {"error": "Bridge error: %s" % e})


def main():
    ap = argparse.ArgumentParser(description="Local bridge between the web remote and your Fire TV.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--tv", help="Fire TV IP address (optional, can be set from the web page)")
    args = ap.parse_args()

    cfg = load_config()
    if args.tv and args.tv != cfg.get("ip"):
        cfg["ip"] = args.tv
        cfg["token"] = ""
        save_config(cfg)

    Handler.cfg = cfg
    Handler.tv = FireTV(cfg)

    try:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as e:
        print("Could not start on port %d: %s" % (args.port, e))
        print("Try: python3 bridge.py --port 8766")
        sys.exit(1)

    print("Fire TV Remote bridge is running.")
    print("  Open the remote:      http://localhost:%d" % args.port)
    print("  From your phone:      http://%s:%d   (same Wi-Fi)" % (local_ip(), args.port))
    print("  Or use the web page:  https://ejq17.github.io/firetv-remote/")
    print("  Fire TV IP:           %s" % (cfg.get("ip") or "(not set yet)"))
    print("  Paired:               %s" % ("yes" if cfg.get("token") else "no"))
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
