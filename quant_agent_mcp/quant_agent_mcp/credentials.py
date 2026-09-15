"""OS-protected credentials; deliberately no plaintext fallback."""
from __future__ import annotations
import base64
import ctypes
import hashlib
import os
import sys
import threading
from pathlib import Path
from typing import Protocol
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from .config import ClientError, state_directory

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def unb64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)

class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...

def protect_windows(data: bytes, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise ClientError("credential_store_unavailable", "Windows DPAPI is unavailable.")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    raw = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # UI_FORBIDDEN, current user scope. LOCAL_MACHINE is deliberately absent.
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ClientError("credential_store_failed", "The OS could not protect or unlock the client credential.")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        free(ctypes.cast(outgoing.pbData, ctypes.c_void_p))

class OSSecretStore:
    def __init__(self, server_url: str, directory: Path | None = None):
        self.scope = hashlib.sha256(server_url.encode()).hexdigest()
        self.directory = directory or state_directory()
        self._lock = threading.RLock()
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
            self.keychain = Keyring()
        elif sys.platform != "win32":
            raise ClientError("unsupported_platform", "An OS-protected store is required.")

    def _name(self, name: str) -> str:
        if name not in {"web_key", "mcp_key", "mcp_invite", "web_invite", "mcp_session", "web_session", "bridge_control"}:
            raise ClientError("invalid_credential_name", "Credential name is not allowed.")
        return self.scope + "-" + name

    def get(self, name: str) -> str | None:
        identity = self._name(name)
        with self._lock:
            if sys.platform == "darwin":
                try:
                    return self.keychain.get_password("QuantAgentClient", identity)
                except Exception as exc:
                    raise ClientError("credential_store_failed", "macOS Keychain could not unlock the credential.") from exc
            target = self.directory / "credentials" / (identity + ".dpapi")
            try:
                return protect_windows(target.read_bytes(), decrypt=True).decode("utf-8")
            except FileNotFoundError:
                return None

    def set(self, name: str, value: str) -> None:
        identity = self._name(name)
        with self._lock:
            if sys.platform == "darwin":
                try:
                    self.keychain.set_password("QuantAgentClient", identity, value)
                    return
                except Exception as exc:
                    raise ClientError("credential_store_failed", "macOS Keychain could not save the credential.") from exc
            root = self.directory / "credentials"
            root.mkdir(parents=True, exist_ok=True)
            target = root / (identity + ".dpapi")
            temporary = root / (identity + "." + b64(os.urandom(9)) + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(protect_windows(value.encode("utf-8")))
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

class DeviceIdentity:
    def __init__(self, store: SecretStore, channel: str):
        if channel not in ("web", "mcp"):
            raise ClientError("invalid_channel", "Device channel must be web or mcp.")
        self.channel, self.store = channel, store
        encoded = store.get(channel + "_key")
        if encoded:
            try:
                self.private_key = Ed25519PrivateKey.from_private_bytes(unb64(encoded))
            except (ValueError, TypeError) as exc:
                raise ClientError("device_credential_invalid", "Device credential is invalid; ask the owner before rebinding.") from exc
        else:
            self.private_key = Ed25519PrivateKey.generate()
            store.set(channel + "_key", b64(self.private_key.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())))

    @property
    def public_key(self) -> str:
        return b64(self.private_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw))

    def sign(self, content: bytes) -> str:
        return b64(self.private_key.sign(content))
