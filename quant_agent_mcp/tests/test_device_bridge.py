import json
import sys
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from quant_agent_mcp.client import request_message
from quant_agent_mcp.config import ClientConfig, ClientError
from quant_agent_mcp.credentials import unb64, protect_windows
from quant_agent_mcp.device_bridge import BrowserDevice, allowed_web_path, make_handler
from test_client import MemoryStore

CONFIG = ClientConfig("https://owner.test/quant-agent")

@pytest.mark.parametrize("method,path", [
    ("POST", "/quant-agent/api/mcp/call"), ("POST", "/quant-agent/api/access/web-session"),
    ("GET", "/quant-agent/api/market/snapshot?x=1"),
    ("GET", "/quant-agent/static/report_visuals/factor_lab/16.png")])
def test_web_allowed_paths(method, path):
    assert allowed_web_path(CONFIG, method, path)

@pytest.mark.parametrize("method,path", [
    ("GET", "https://evil.test/api/x"), ("GET", "//evil.test/api/x"),
    ("POST", "/quant-agent/api/admin/approve"), ("GET", "/quant-agent/admin"),
    ("POST", "/quant-agent/api/access/activate"),
    ("GET", "/quant-agent/api/access/challenge"), ("GET", "/api/x"),
    ("GET", "/quant-agent/api/%61dmin/approve"), ("GET", "/quant-agent/api/../admin"),
    ("GET", "/quant-agent/api/x#fragment"), ("DELETE", "/quant-agent/api/task"),
    ("GET", "/quant-agent\\api/x")])
def test_web_disallowed_paths(method, path):
    assert not allowed_web_path(CONFIG, method, path)

def test_browser_signing_exact_body():
    device = BrowserDevice(CONFIG, MemoryStore())
    result = device.sign({"method": "POST", "path": "/quant-agent/api/mcp/call",
        "body": {"tool": "query", "arguments": {"word": "\u4e2d\u6587"}}, "access_token": "web-token"})
    headers = result["headers"]
    Ed25519PublicKey.from_public_bytes(unb64(device.identity.public_key)).verify(
        unb64(headers["X-Quant-Signature"]), request_message("POST",
            "/quant-agent/api/mcp/call", result["body_text"].encode("utf-8"),
            "web-token", headers["X-Quant-Timestamp"], headers["X-Quant-Nonce"]))
    assert json.loads(result["body_text"])["arguments"]["word"] == "\u4e2d\u6587"

def test_loopback_origin_host_and_preflight():
    device = BrowserDevice(CONFIG, MemoryStore())
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(device))
    device.config = replace(CONFIG, bridge_port=server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            url = f"http://127.0.0.1:{server.server_port}/v1/health"
            assert client.get(url).status_code == 403
            assert client.get(url, headers={"Origin": "https://evil.test"}).status_code == 403
            assert client.get(url, headers={"Origin": CONFIG.origin, "Host": "evil.test"}).status_code == 403
            response = client.get(url, headers={"Origin": CONFIG.origin})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == CONFIG.origin
            assert "private_key" not in response.text
            response = client.options(url, headers={"Origin": CONFIG.origin,
                "Access-Control-Request-Private-Network": "true"})
            assert response.headers["access-control-allow-private-network"] == "true"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()

@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI requires Windows")
def test_windows_dpapi_roundtrip_without_files():
    raw = b"test-only-device-secret"
    protected = protect_windows(raw)
    assert raw not in protected
    assert protect_windows(protected, decrypt=True) == raw


def test_chinese_report_path_uses_actual_encoded_url():
    from urllib.parse import quote
    device = BrowserDevice(CONFIG, MemoryStore())
    original = "/quant-agent/static/report_visuals/factor_lab/16_\u6df1\u5c42MLP.png"
    result = device.sign({"method": "GET", "path": original, "access_token": "web-token"})
    assert result["path"] == quote(original, safe="/-._~")
    encoded = device.sign({"method": "GET", "path": result["path"], "access_token": "web-token"})
    assert encoded["path"] == result["path"]
    headers = result["headers"]
    Ed25519PublicKey.from_public_bytes(unb64(device.identity.public_key)).verify(
        unb64(headers["X-Quant-Signature"]), request_message("GET", result["path"], b"",
            "web-token", headers["X-Quant-Timestamp"], headers["X-Quant-Nonce"]))

@pytest.mark.parametrize("path", ["/quant-agent/api/x%2fy", "/quant-agent/api/%252e%252e/admin",
    "/quant-agent/api/%00", "/quant-agent/api/%GG"])
def test_ambiguous_encoded_paths_are_rejected(path):
    assert not allowed_web_path(CONFIG, "GET", path)


def test_personal_analysis_and_exact_artifact_routes_keep_admin_denied():
    path = "/quant-agent/api/access/jobs/" + "a" * 32 + "/artifacts/" + "b" * 32
    assert allowed_web_path(CONFIG, "POST", "/quant-agent/api/ai/analyze")
    assert allowed_web_path(CONFIG, "GET", path + "?preview=1")
    for method, rejected in (
            ("POST", path), ("GET", path.replace("a" * 32, "../admin")),
            ("GET", path + "/extra"), ("GET", "/quant-agent/api/access/jobs"),
            ("POST", "/quant-agent/api/ai/analyze/approve"),
            ("POST", "/quant-agent/api/admin/approvals")):
        assert not allowed_web_path(CONFIG, method, rejected)
