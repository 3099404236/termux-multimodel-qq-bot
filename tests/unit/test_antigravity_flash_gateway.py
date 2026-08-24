import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _load_gateway_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "deployment"
        / "vps"
        / "antigravity_flash_gateway.py"
    )
    spec = importlib.util.spec_from_file_location(
        "antigravity_flash_gateway_test", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load gateway module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATEWAY = _load_gateway_module()


def _start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_gateway_forces_model_and_replaces_manager_credentials():
    captured = {}

    class UpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            captured["path"] = self.path
            captured["authorization"] = self.headers["Authorization"]
            captured["payload"] = json.loads(body)
            response = json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    _start_server(upstream)
    config = GATEWAY.GatewayConfig(
        client_api_key="client-key-with-at-least-24-chars",
        manager_api_key="private-manager-key",
        manager_base_url=f"http://127.0.0.1:{upstream.server_port}",
        target_model="gemini-3.7-flash-high",
    )
    gateway = GATEWAY.GatewayServer(("127.0.0.1", 0), GATEWAY.build_handler(config))
    _start_server(gateway)

    try:
        body = json.dumps(
            {
                "model": "claude-opus-4-6-thinking",
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{gateway.server_port}/v1/chat/completions?beta=true",
            data=body,
            headers={
                "Authorization": "Bearer client-key-with-at-least-24-chars",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.load(response)["choices"][0]["message"]["content"] == "ok"
    finally:
        gateway.shutdown()
        upstream.shutdown()

    assert captured["path"] == "/v1/chat/completions?beta=true"
    assert captured["authorization"] == "Bearer private-manager-key"
    assert captured["payload"]["model"] == "gemini-3.7-flash-high"


def test_gateway_rejects_invalid_key_and_only_lists_target_model():
    config = GATEWAY.GatewayConfig(
        client_api_key="client-key-with-at-least-24-chars",
        manager_api_key="private-manager-key",
        manager_base_url="http://127.0.0.1:9",
        target_model="gemini-3.7-flash-high",
    )
    gateway = GATEWAY.GatewayServer(("127.0.0.1", 0), GATEWAY.build_handler(config))
    _start_server(gateway)
    try:
        unauthorized = urllib.request.Request(
            f"http://127.0.0.1:{gateway.server_port}/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )
        try:
            urllib.request.urlopen(unauthorized, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("invalid client key unexpectedly succeeded")

        authorized = urllib.request.Request(
            f"http://127.0.0.1:{gateway.server_port}/v1/models",
            headers={"Authorization": "Bearer client-key-with-at-least-24-chars"},
        )
        with urllib.request.urlopen(authorized, timeout=5) as response:
            models = json.load(response)
    finally:
        gateway.shutdown()

    assert [model["id"] for model in models["data"]] == ["gemini-3.7-flash-high"]


def test_upstream_url_accepts_manager_base_ending_in_v1():
    assert (
        GATEWAY._upstream_url(
            "https://manager.example/proxy/v1/",
            "/v1/chat/completions?beta=true",
        )
        == "https://manager.example/proxy/v1/chat/completions?beta=true"
    )
