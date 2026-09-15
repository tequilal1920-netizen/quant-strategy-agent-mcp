"""HTTPS-only calls to one configured server; no local model or data access."""
from __future__ import annotations
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
import httpx
from .config import ClientConfig, ClientError, load_config
from .credentials import DeviceIdentity, OSSecretStore, SecretStore

TOOLS = frozenset({"catalog", "query", "model_result", "db_catalog", "db_query",
    "job_submit", "job_list", "job_status", "job_result", "job_cancel", "job_share",
    "job_fork", "job_delete", "job_revoke_share", "job_artifacts", "artifact_read", "permissions", "health", "code_save", "code_read", "approval_request"})
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

def encode_body(value: dict[str, Any] | None) -> bytes:
    return b"" if value is None else json.dumps(value, ensure_ascii=False,
        separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")

def request_message(method: str, path: str, body: bytes, token: str,
                    timestamp: str, nonce: str) -> bytes:
    return ("quant-agent-request-v1\n" + method.upper() + "\n" + path + "\n"
        + hashlib.sha256(body).hexdigest() + "\n"
        + hashlib.sha256(token.encode("utf-8")).hexdigest() + "\n"
        + timestamp + "\n" + nonce).encode("utf-8")

def proof_headers(identity: DeviceIdentity, method: str, path: str, body: bytes,
                  token: str, *, timestamp: str | None = None, nonce: str | None = None) -> dict[str, str]:
    if not token or "\n" in token or "\r" in token or len(token) > 8192:
        raise ClientError("invalid_access_token", "An access token is required.")
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or secrets.token_urlsafe(24)
    return {"Authorization": "Bearer " + token, "X-Quant-Timestamp": timestamp,
        "X-Quant-Nonce": nonce, "X-Quant-Signature": identity.sign(
            request_message(method, path, body, token, timestamp, nonce)),
        "Content-Type": "application/json"}

def expiry_time(value: Any) -> float:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0

class RemoteClient:
    def __init__(self, config: ClientConfig | None = None, *, store: SecretStore | None = None,
                 channel: str = "mcp", transport: httpx.BaseTransport | None = None):
        self.config = config or load_config()
        self.store = store or OSSecretStore(self.config.server_url)
        self.identity = DeviceIdentity(self.store, channel)
        self.channel = channel
        self.http = httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0),
            follow_redirects=False, verify=True, trust_env=False, transport=transport)

    def close(self):
        self.http.close()

    def _request(self, suffix: str, payload: dict, headers: dict | None = None) -> dict:
        raw = encode_body(payload)
        try:
            with self.http.stream("POST", self.config.url(suffix), content=raw,
                    headers=headers or {"Content-Type": "application/json"}) as response:
                if response.is_redirect:
                    raise ClientError("redirect_refused", "The server redirected the request; verify its configured URL.")
                chunks, total = [], 0
                for part in response.iter_bytes():
                    total += len(part)
                    if total > MAX_RESPONSE_BYTES:
                        raise ClientError("response_too_large", "The result exceeds the response limit; query a smaller range.")
                    chunks.append(part)
                if "application/json" not in response.headers.get("content-type", "").lower():
                    raise ClientError("invalid_server_response", "The server did not return structured JSON.")
                try:
                    data = json.loads(b"".join(chunks))
                except (ValueError, UnicodeError) as exc:
                    raise ClientError("invalid_server_response", "The server returned invalid JSON.") from exc
                if not isinstance(data, dict):
                    raise ClientError("invalid_server_response", "The server returned an unexpected JSON structure.")
                if response.status_code >= 400:
                    error = data.get("error", {})
                    code = error.get("code") if isinstance(error, dict) else error
                    code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code) else "remote_request_rejected"
                    message = {401: "Authentication expired or was rejected; activate this device again.",
                        403: "The owner has not granted access to this operation.",
                        409: "The request conflicts with the current device or task state.",
                        429: "The server is busy; check existing tasks before retrying."}.get(
                            response.status_code, "The server rejected the request. Contact the owner with the error code.")
                    raise ClientError(code, message)
                return data
        except httpx.TransportError as exc:
            raise ClientError("server_unreachable", "Could not reach the configured server. A submitted task may still exist; check its status before resubmitting.") from exc

    def activate(self, invite_code: str, *, remember: bool = True) -> dict:
        if not isinstance(invite_code, str) or not 1 <= len(invite_code.strip()) <= 512:
            raise ClientError("invalid_invite", "Enter the invitation issued by the owner.")
        invitation = invite_code.strip()
        challenge = self._request("/api/access/challenge", {"invite_code": invitation,
            "channel": self.channel, "device_public_key": self.identity.public_key})
        if challenge.get("channel") != self.channel or challenge.get("device_public_key") != self.identity.public_key:
            raise ClientError("invalid_challenge", "The server challenge does not match this device.")
        cid, nonce = challenge.get("challenge_id"), challenge.get("challenge")
        if (not isinstance(cid, str) or not isinstance(nonce, str) or not cid or not nonce
                or any(c in cid + nonce for c in "\r\n") or len(cid) > 256 or len(nonce) > 512
                or expiry_time(challenge.get("expires_at")) <= time.time()):
            raise ClientError("invalid_challenge", "The server challenge is invalid or expired.")
        message = f"quant-agent-device-v1\n{cid}\n{nonce}\n{self.channel}".encode("utf-8")
        result = self._request("/api/access/activate", {"challenge_id": cid,
            "signature": self.identity.sign(message)})
        if (result.get("ok") is not True or result.get("channel") != self.channel
                or not all(isinstance(result.get(key), str) and result[key] for key in ("access_token", "account_id", "device_id"))
                or expiry_time(result.get("expires_at")) <= time.time()):
            raise ClientError("activation_failed", "The server did not confirm activation.")
        if remember:
            self.store.set(self.channel + "_session", json.dumps(result))
            self.store.set(self.channel + "_invite", invitation)
        return result

    def session(self, *, refresh: bool = False) -> dict:
        """Recover or renew only this channel using its OS-protected invite."""
        if type(refresh) is not bool:
            raise ClientError("invalid_session_request", "refresh must be a boolean.")
        saved = self.store.get(self.channel + "_session")
        try:
            session = json.loads(saved) if saved else {}
        except (ValueError, TypeError):
            session = {}
        if (not refresh and isinstance(session, dict) and session.get("ok") is True and session.get("channel") == self.channel
                and all(isinstance(session.get(key), str) and session[key] for key in ("account_id", "device_id"))
                and expiry_time(session.get("expires_at")) > time.time() + 30
                and isinstance(session.get("access_token"), str) and session["access_token"]):
            return session
        invitation = self.store.get(self.channel + "_invite")
        if not invitation:
            raise ClientError("device_not_activated", "Enter your invitation privately to activate this device.")
        return self.activate(invitation)

    def _token(self) -> str:
        return self.session()["access_token"]

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict:
        if tool not in TOOLS:
            raise ClientError("unknown_tool", "This client does not expose that operation.")
        if self.channel != "mcp":
            raise ClientError("invalid_channel", "Use the web authentication bridge for the web channel.")
        body = {"tool": tool, "arguments": arguments or {}}
        token = self._token()
        suffix = "/api/mcp/call"
        headers = proof_headers(self.identity, "POST", self.config.prefix + suffix,
                                encode_body(body), token)
        result = self._request(suffix, body, headers)
        if not isinstance(result.get("ok"), bool):
            raise ClientError("invalid_server_response", "The server omitted the operation result status.")
        return result
