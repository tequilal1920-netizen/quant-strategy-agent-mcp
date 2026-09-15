"""Loopback-only browser device proof. Never proxies arbitrary hosts or admin calls."""
from __future__ import annotations
import json
import argparse
import hmac
import os
import secrets
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote_to_bytes, quote
import re
from .client import RemoteClient, encode_body, proof_headers
from .config import ClientConfig, ClientError, load_config
from .credentials import DeviceIdentity, OSSecretStore, SecretStore

MAX_BODY = 1024 * 1024

def normalized_web_path(config: ClientConfig, method: str, path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ClientError("web_path_forbidden", "An origin-relative website path is required.")
    try:
        parts = urlsplit(path)
    except ValueError as exc:
        raise ClientError("web_path_forbidden", "Invalid website path.") from exc
    if parts.scheme or parts.netloc or parts.fragment or "\\" in path or re.search(r"%(?![0-9a-fA-F]{2})", path):
        raise ClientError("web_path_forbidden", "Invalid website path.")
    segments = []
    try:
        for raw in parts.path.split("/"):
            segment = unquote_to_bytes(raw).decode("utf-8", errors="strict")
            if segment in (".", "..") or any(c in segment for c in ("/", "\\", "%", "\x00", "\r", "\n")):
                raise ValueError("Unsafe path segment")
            segments.append(segment)
    except (ValueError, UnicodeError) as exc:
        raise ClientError("web_path_forbidden", "Encoded traversal or ambiguous path is forbidden.") from exc
    clean = "/".join(segments)
    if not clean.startswith(config.prefix + "/"):
        raise ClientError("web_path_forbidden", "The path is outside the configured website.")
    relative = clean[len(config.prefix):]
    if any("admin" in segment.lower() for segment in relative.split("/")):
        raise ClientError("web_path_forbidden", "The device cannot sign administrator requests.")
    artifact_path = re.fullmatch(r"/api/access/jobs/[0-9a-f]{32}/artifacts/[0-9a-f]{32}", relative) is not None
    allowed = (method == "POST" and relative in ("/api/mcp/call", "/api/access/web-session", "/api/ai/analyze")) or (
        method == "GET" and ((relative.startswith("/api/") and not relative.startswith("/api/access/"))
                            or relative.startswith("/static/report_visuals/") or artifact_path))
    if not allowed:
        raise ClientError("web_path_forbidden", "The device cannot sign that website operation.")
    result = quote(clean, safe="/-._~")
    if parts.query:
        result += "?" + quote(parts.query, safe="!$&'()*+,-./:;=?@_%~")
    return result


def allowed_web_path(config: ClientConfig, method: str, path: str) -> bool:
    try:
        normalized_web_path(config, method, path)
        return True
    except ClientError:
        return False

class BrowserDevice:
    def __init__(self, config: ClientConfig, store: SecretStore | None = None,
                 remote_factory=RemoteClient):
        self.config = config
        self.store = store or OSSecretStore(config.server_url)
        self.identity = DeviceIdentity(self.store, "web")
        self.remote_factory = remote_factory
        self.activation_lock = threading.Lock()
        self.control_token = self.store.get("bridge_control")
        if not self.control_token:
            self.control_token = secrets.token_urlsafe(32)
            self.store.set("bridge_control", self.control_token)

    def activate(self, invitation: str) -> dict:
        with self.activation_lock:
            client = self.remote_factory(self.config, store=self.store, channel="web")
            try:
                return client.activate(invitation)
            finally:
                client.close()

    def session(self, *, refresh=False) -> dict:
        with self.activation_lock:
            client = self.remote_factory(self.config, store=self.store, channel="web")
            try:
                result = client.session(refresh=refresh)
                return {key: result[key] for key in ("ok", "access_token", "expires_at", "account_id", "device_id", "channel")}
            finally:
                client.close()

    def sign(self, payload: dict) -> dict:
        method = payload.get("method", "")
        path = payload.get("path")
        if not isinstance(method, str):
            raise ClientError("web_path_forbidden", "The device cannot sign that request.")
        method = method.upper()
        path = normalized_web_path(self.config, method, path)
        body = payload.get("body")
        if method == "GET" and body not in (None, {}):
            raise ClientError("invalid_web_body", "GET requests cannot contain a body.")
        if body is not None and not isinstance(body, dict):
            raise ClientError("invalid_web_body", "Request body must be a JSON object.")
        raw = encode_body(None if method == "GET" else (body or {}))
        if len(raw) > MAX_BODY:
            raise ClientError("request_too_large", "The request is too large.")
        headers = proof_headers(self.identity, method, path, raw, payload.get("access_token", ""))
        return {"ok": True, "path": path, "headers": headers, "body_text": raw.decode("utf-8")}

def make_handler(device: BrowserDevice):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QuantAgentDevice/0.3"
        sys_version = ""

        def log_message(self, format, *args):
            # No invitation, token, request body or browsing history in local logs.
            pass

        def _allowed(self):
            return (self.headers.get("Host") == f"127.0.0.1:{device.config.bridge_port}"
                and self.headers.get("Origin") == device.config.origin)

        def _reply(self, status, data):
            raw = encode_body(data)
            self.send_response(status)
            if self._allowed():
                self.send_header("Access-Control-Allow-Origin", device.config.origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def _check(self):
            if not self._allowed():
                # Drain a bounded POST body before sending the rejection so
                # Windows does not reset a socket with unread request bytes.
                if self.command == "POST":
                    try:
                        count = int(self.headers.get("Content-Length", "0"))
                        if not self.headers.get("Transfer-Encoding") and 0 < count <= MAX_BODY:
                            self.connection.settimeout(2)
                            self.rfile.read(count)
                    except (ValueError, TypeError, OSError):
                        pass
                self._reply(403, {"ok": False, "error": {"code": "origin_forbidden",
                    "message": "This origin is not permitted."}})
                return False
            return True

        def do_OPTIONS(self):
            if self._check():
                self._reply(200, {"ok": True})

        def do_GET(self):
            if not self._check():
                return
            if self.path != "/v1/health":
                self._reply(404, {"ok": False, "error": {"code": "not_found", "message": "Not found."}})
                return
            self._reply(200, {"ok": True, "version": "0.3.0", "channel": "web",
                "server_origin": device.config.origin, "server_url": device.config.server_url,
                "protocol": "quant-agent-device/1", "device_public_key": device.identity.public_key})

        def do_POST(self):
            if self.path == "/v1/control/stop":
                # Browser requests cannot use this endpoint: no Origin is
                # accepted and its local control secret is never returned.
                # Consume only the bounded control body before closing. On
                # Windows closing a socket with unread POST bytes can reset it
                # before the client receives the structured rejection.
                try:
                    count = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get("Transfer-Encoding") or not 0 <= count <= 1024:
                        raise ValueError()
                    self.connection.settimeout(2)
                    raw = self.rfile.read(count)
                    if raw and json.loads(raw) != {}:
                        raise ValueError()
                except (ValueError, TypeError, OSError):
                    self._reply(400, {"ok": False, "error": {"code": "invalid_request", "message": "Use an empty control request."}})
                    return
                supplied = self.headers.get("X-Quant-Device-Control", "")
                if (self.headers.get("Host") != f"127.0.0.1:{device.config.bridge_port}"
                        or self.headers.get("Origin") is not None
                        or not hmac.compare_digest(supplied, device.control_token)):
                    self._reply(403, {"ok": False, "error": {"code": "control_denied", "message": "Local control denied."}})
                    return
                self._reply(200, {"ok": True, "status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if not self._check():
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ClientError("invalid_request", "Chunked requests are not accepted.")
                count = int(self.headers.get("Content-Length", "0"))
                if count <= 0 or count > MAX_BODY:
                    raise ClientError("request_too_large", "The request body is missing or too large.")
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    raise ClientError("invalid_content_type", "Use application/json.")
                payload = json.loads(self.rfile.read(count))
                if not isinstance(payload, dict):
                    raise ClientError("invalid_request", "Use a JSON object.")
                if self.path == "/v1/web-activate":
                    result = device.activate(payload.get("invite_code", ""))
                elif self.path == "/v1/web-session":
                    if set(payload) - {"refresh"} or type(payload.get("refresh", False)) is not bool:
                        raise ClientError("invalid_session_request", "Only a boolean refresh option is accepted.")
                    result = device.session(refresh=payload.get("refresh", False))
                elif self.path == "/v1/web-sign":
                    result = device.sign(payload)
                else:
                    raise ClientError("not_found", "Not found.")
                self._reply(200, result)
            except ClientError as exc:
                self._reply(400, exc.result())
            except (ValueError, TypeError, OSError):
                self._reply(400, {"ok": False, "error": {"code": "invalid_request",
                    "message": "The device could not process this request."}})

    return Handler

def supervise() -> int:
    """Restart an unexpectedly crashed child; an intentional clean stop exits."""
    failures = []
    while True:
        started = time.monotonic()
        child = subprocess.Popen([sys.executable, "-I", "-B", "-m", "quant_agent_mcp.device_bridge", "--child"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            return 0
        if code in (0, 75):
            return code
        failures = [at for at in failures if time.monotonic() - at < 60]
        failures.append(started)
        if len(failures) >= 5:
            return code
        time.sleep(min(0.5 * (2 ** (len(failures) - 1)), 5))

def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--supervise", action="store_true")
    group.add_argument("--child", action="store_true")
    args = parser.parse_args(argv)
    if args.supervise:
        return supervise()
    config = load_config()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", config.bridge_port),
            make_handler(BrowserDevice(config)))
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or exc.errno in (48, 98):
            return 75
        raise
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
