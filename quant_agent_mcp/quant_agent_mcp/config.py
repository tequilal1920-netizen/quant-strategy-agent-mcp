"""Configuration contains no model paths, credentials or fallback servers."""
from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BRIDGE_PORT = 47631

class ClientError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def result(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message}}

def state_directory() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise ClientError("local_state_unavailable", "LOCALAPPDATA is unavailable.")
        return Path(base) / "QuantAgentClient"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "QuantAgentClient"
    raise ClientError("unsupported_platform", "This client supports Windows and macOS.")

@dataclass(frozen=True)
class ClientConfig:
    server_url: str
    bridge_port: int = BRIDGE_PORT

    def __post_init__(self):
        value = self.server_url.rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or "\\" in value or "%" in parsed.path
                or any(part in (".", "..") for part in parsed.path.split("/"))):
            raise ClientError("invalid_server_url", "Use the owner's HTTPS server URL without credentials, query or fragment.")
        try:
            parsed.port
        except ValueError as exc:
            raise ClientError("invalid_server_url", "Invalid HTTPS port.") from exc
        if not 1024 <= self.bridge_port <= 65535:
            raise ClientError("invalid_bridge_port", "Local port must be between 1024 and 65535.")
        object.__setattr__(self, "server_url", value)

    @property
    def origin(self) -> str:
        p = urlsplit(self.server_url)
        return f"{p.scheme}://{p.netloc}"

    @property
    def prefix(self) -> str:
        return urlsplit(self.server_url).path.rstrip("/")

    def url(self, suffix: str) -> str:
        if not suffix.startswith("/") or suffix.startswith("//"):
            raise ClientError("invalid_endpoint", "Endpoint must be relative to the configured server.")
        return self.server_url + suffix

def load_config(directory: Path | None = None, *, allow_legacy_port=False) -> ClientConfig:
    target = (directory or state_directory()) / "client.json"
    try:
        content = json.loads(target.read_text(encoding="utf-8"))
        config = ClientConfig(server_url=content["server_url"], bridge_port=content.get("bridge_port", BRIDGE_PORT))
        if config.bridge_port != BRIDGE_PORT and not allow_legacy_port:
            raise ClientError("unsupported_bridge_port", "The website requires port 47631; run configure again with the default port.")
        return config
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ClientError("client_not_configured", "Run quant-agent-client configure with the owner's server URL.") from exc

def save_config(config: ClientConfig, directory: Path | None = None) -> None:
    if config.bridge_port != BRIDGE_PORT:
        raise ClientError("unsupported_bridge_port", "The website requires the fixed local port 47631.")
    root = directory or state_directory()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "client.json"
    temporary = root / "client.json.new"
    try:
        temporary.write_text(json.dumps({"server_url": config.server_url,
            "bridge_port": config.bridge_port}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
