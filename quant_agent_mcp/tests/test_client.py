import asyncio
import hashlib
import json
import time
import tomllib
from pathlib import Path
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from quant_agent_mcp.client import RemoteClient, encode_body, proof_headers, request_message, TOOLS
from quant_agent_mcp.config import ClientConfig, ClientError
from quant_agent_mcp.credentials import DeviceIdentity, unb64
import quant_agent_mcp
from quant_agent_mcp import server

def test_package_version_matches_project_metadata():
    metadata = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert quant_agent_mcp.__version__ == metadata["project"]["version"]

def test_public_docs_keep_live_research_and_retained_result_channels_distinct():
    public_root = Path(__file__).resolve().parents[2]
    readme = (public_root / "README.md").read_text(encoding="utf-8")
    protocol = (public_root / "PROTOCOL.md").read_text(encoding="utf-8")
    compact = "".join((readme + protocol).split())

    assert '"operation":"scheduled/latest"' in compact
    assert '"operation":"scheduled/research"' in compact
    assert "quant-agent-current-signal/1.0" in readme and "quant-agent-current-signal/1.0" in protocol
    for field in ("signal.eligible", "signal.blockers", "schedule.signal_sha256"):
        assert field in readme and field in protocol
    for marker in ('channel="research"', "research=true", "is_live=false",
                   "header.research_result_is_live=false"):
        assert marker in readme and marker in protocol
    assert "`model_result` 永远不能作为 live latest 的判定入口" in readme
    assert "`model_result` 不属于调度 live 通道" in protocol
    assert "/api/admin/" not in readme + protocol

class MemoryStore:
    def __init__(self):
        self.values = {}
    def get(self, name):
        return self.values.get(name)
    def set(self, name, value):
        self.values[name] = value

def test_distinct_channel_keys_persist():
    store = MemoryStore()
    web, mcp = DeviceIdentity(store, "web"), DeviceIdentity(store, "mcp")
    assert web.public_key != mcp.public_key
    assert DeviceIdentity(store, "web").public_key == web.public_key
    assert DeviceIdentity(store, "mcp").public_key == mcp.public_key

@pytest.mark.parametrize("url", ["http://owner.test", "https://user:pass@owner.test",
    "https://owner.test/a?x=1", "https://owner.test/a#x", "https://owner.test/../a",
    "https://owner.test/%2e%2e", "https://owner.test:bad"])
def test_unsafe_server_urls_rejected(url):
    with pytest.raises(ClientError):
        ClientConfig(url)

def test_activation_and_signed_query_round_trip():
    store, seen = MemoryStore(), []
    config = ClientConfig("https://owner.test/quant-agent")
    client = None
    token = "test-session-token"
    def handle(request):
        nonlocal client
        seen.append(request)
        data = json.loads(request.content)
        if request.url.path.endswith("/challenge"):
            assert data["channel"] == "mcp"
            assert data["invite_code"] == "test-invitation"
            return httpx.Response(200, json={"challenge_id": "id-1", "challenge": "challenge-1",
                "expires_at": time.time()+120, "channel": "mcp",
                "device_public_key": client.identity.public_key})
        if request.url.path.endswith("/activate"):
            Ed25519PublicKey.from_public_bytes(unb64(client.identity.public_key)).verify(
                unb64(data["signature"]), b"quant-agent-device-v1\nid-1\nchallenge-1\nmcp")
            return httpx.Response(200, json={"ok": True, "access_token": token, "account_id": "acct-test",
                "device_id": "device-test", "channel": "mcp", "expires_at": time.time()+43200})
        assert request.url.path == "/quant-agent/api/mcp/call"
        assert request.headers["authorization"] == "Bearer " + token
        message = request_message("POST", request.url.raw_path.decode(), request.content,
            token, request.headers["x-quant-timestamp"], request.headers["x-quant-nonce"])
        Ed25519PublicKey.from_public_bytes(unb64(client.identity.public_key)).verify(
            unb64(request.headers["x-quant-signature"]), message)
        assert data == {"tool": "query", "arguments": {"module": "asset", "operation": "current"}}
        return httpx.Response(200, json={"ok": True, "data": {"model_id": "asset-final", "value": 1.23}})
    client = RemoteClient(config, store=store, transport=httpx.MockTransport(handle))
    assert client.activate("test-invitation")["account_id"] == "acct-test"
    assert store.get("mcp_invite") == "test-invitation"
    result = client.call("query", {"module": "asset", "operation": "current"})
    assert result["data"]["value"] == 1.23
    assert len(seen) == 3
    client.close()

def test_signature_binds_body_path_token_and_nonce():
    identity = DeviceIdentity(MemoryStore(), "mcp")
    raw = encode_body({"text": "\u4e2d\u6587", "a": 3})
    headers = proof_headers(identity, "POST", "/quant-agent/api/mcp/call", raw,
        "token", timestamp="12345", nonce="nonce")
    public = Ed25519PublicKey.from_public_bytes(unb64(identity.public_key))
    public.verify(unb64(headers["X-Quant-Signature"]),
        request_message("POST", "/quant-agent/api/mcp/call", raw, "token", "12345", "nonce"))
    from cryptography.exceptions import InvalidSignature
    for path, body, token, nonce in [
        ("/api/mcp/call", raw, "token", "nonce"),
        ("/quant-agent/api/mcp/call", b"{}", "token", "nonce"),
        ("/quant-agent/api/mcp/call", raw, "other", "nonce"),
        ("/quant-agent/api/mcp/call", raw, "token", "other")]:
        with pytest.raises(InvalidSignature):
            public.verify(unb64(headers["X-Quant-Signature"]),
                request_message("POST", path, body, token, "12345", nonce))

@pytest.mark.parametrize("response,code", [
    (httpx.Response(302, headers={"Location": "https://evil.test"}), "redirect_refused"),
    (httpx.Response(200, text="<html>Login</html>", headers={"content-type": "text/html"}), "invalid_server_response"),
    (httpx.Response(403, json={"error": {"code": "access_denied", "message": "secret-or-path"}}), "access_denied")])
def test_remote_failure_does_not_leak_raw_response(response, code):
    client = RemoteClient(ClientConfig("https://owner.test"), store=MemoryStore(),
        transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(ClientError) as caught:
        client._request("/api/access/challenge", {})
    assert caught.value.code == code
    assert "secret-or-path" not in str(caught.value)
    client.close()

def test_mismatched_challenge_never_activates():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"challenge_id": "x", "challenge": "n",
            "channel": "web", "device_public_key": "wrong", "expires_at": time.time()+120})
    client = RemoteClient(ClientConfig("https://owner.test"), store=MemoryStore(),
        transport=httpx.MockTransport(handle))
    with pytest.raises(ClientError, match="does not match"):
        client.activate("test-invitation")
    assert len(seen) == 1
    assert client.store.get("mcp_invite") is None
    client.close()

def test_tools_are_remote_only_and_structured(monkeypatch):
    listed = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in listed}
    assert names == TOOLS
    assert not names.intersection({"read_model_doc", "search_model_text", "learning_path",
        "approve", "deploy", "confirm"})
    assert all(tool.outputSchema for tool in listed)
    calls = []
    monkeypatch.setattr(server, "_call", lambda tool, args=None: calls.append((tool, args)) or {"ok": True})
    assert server.code_save("job-1", "research.py", "x=1") == {"ok": True}
    assert calls == [("code_save", {"job_id": "job-1", "name": "research.py", "code": "x=1"})]


def test_tool_contract_preserves_job_identity_and_export_intent(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_call", lambda tool, args=None: calls.append((tool, args)) or {"ok": True})
    server.job_list()
    server.job_submit("research.run", {"code_job_id": "source", "inputs": []}, "once-only")
    server.job_fork("source")
    server.code_read("job-1", "research.py")
    server.job_result("job-1", download=True)
    server.model_result("model-1", "frozen-v1", fields=["returns"], download=True)
    server.db_query("warehouse/prices", ["date", "close"], download=True)
    assert calls[:5] == [
        ("job_list", None),
        ("job_submit", {"kind": "research.run", "spec": {"code_job_id": "source", "inputs": []}, "idempotency_key": "once-only"}),
        ("job_fork", {"job_id": "source"}),
        ("code_read", {"job_id": "job-1", "name": "research.py"}),
        ("job_result", {"job_id": "job-1", "download": True})]
    assert calls[5][1]["result_version"] == "frozen-v1"
    assert calls[5][1]["download"] is True
    assert calls[6][1]["download"] is True and calls[6][1].get("cursor") is None
    listed = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert len(listed) == 21
    assert "spec" not in listed["job_fork"].inputSchema["properties"]
    assert set(listed["code_save"].inputSchema["required"]) == {"job_id", "name", "code"}
    assert "idempotency_key" in listed["job_submit"].inputSchema["required"]

def test_database_cursor_is_forwarded_without_changing_query_or_export(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_call", lambda tool, args=None: calls.append((tool, args)) or {"ok": True})
    server.db_query("warehouse/prices", ["date", "close"], filters={"asset": "A"},
        date_from="2026-01-01", date_to="2026-06-30", limit=10000, download=True,
        cursor="opaque-continuation-fixture")
    assert calls == [("db_query", {"dataset": "warehouse/prices", "fields": ["date", "close"],
        "filters": {"asset": "A"}, "date_from": "2026-01-01", "date_to": "2026-06-30",
        "limit": 10000, "download": True, "cursor": "opaque-continuation-fixture"})]
    listed = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert "cursor" in listed["db_query"].inputSchema["properties"]
    assert "cursor" not in listed["db_query"].inputSchema.get("required", [])
