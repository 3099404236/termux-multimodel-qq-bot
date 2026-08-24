#!/usr/bin/env python3
"""Single-model OpenAI/Anthropic gateway for Antigravity Manager.

The public client key is intentionally not a Manager token. Every accepted
request is authenticated here, its model is overwritten with TARGET_MODEL,
and it is forwarded with the private Manager API key.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOGGER = logging.getLogger("antigravity-flash-gateway")
ALLOWED_POST_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
    }
)
FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "anthropic-beta",
        "anthropic-version",
        "openai-beta",
        "user-agent",
        "x-request-id",
    }
)
FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-type",
        "openai-processing-ms",
        "request-id",
        "x-request-id",
    }
)


@dataclass(frozen=True)
class GatewayConfig:
    client_api_key: str
    manager_api_key: str
    manager_base_url: str
    target_model: str
    max_body_bytes: int = 16 * 1024 * 1024
    upstream_timeout_seconds: float = 600


def load_config() -> tuple[str, int, GatewayConfig]:
    host = os.environ.get("FLASH_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASH_GATEWAY_PORT", "18046"))
    config = GatewayConfig(
        client_api_key=os.environ.get("FLASH_GATEWAY_API_KEY", ""),
        manager_api_key=os.environ.get("ANTIGRAVITY_MANAGER_API_KEY", ""),
        manager_base_url=os.environ.get(
            "ANTIGRAVITY_MANAGER_BASE_URL", "http://127.0.0.1:8045"
        ),
        target_model=os.environ.get("FLASH_GATEWAY_TARGET_MODEL", ""),
        max_body_bytes=int(
            os.environ.get("FLASH_GATEWAY_MAX_BODY_BYTES", str(16 * 1024 * 1024))
        ),
        upstream_timeout_seconds=float(os.environ.get("FLASH_GATEWAY_TIMEOUT", "600")),
    )
    validate_config(config)
    return host, port, config


def validate_config(config: GatewayConfig) -> None:
    if len(config.client_api_key) < 24:
        raise ValueError("FLASH_GATEWAY_API_KEY must contain at least 24 characters")
    if not config.manager_api_key:
        raise ValueError("ANTIGRAVITY_MANAGER_API_KEY is required")
    parsed = urllib.parse.urlsplit(config.manager_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ANTIGRAVITY_MANAGER_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "ANTIGRAVITY_MANAGER_BASE_URL must not contain credentials or query data"
        )
    if not config.target_model or re.search(r"\s", config.target_model):
        raise ValueError("FLASH_GATEWAY_TARGET_MODEL must be one model ID")
    if config.max_body_bytes < 1:
        raise ValueError("FLASH_GATEWAY_MAX_BODY_BYTES must be positive")
    if config.upstream_timeout_seconds < 1:
        raise ValueError("FLASH_GATEWAY_TIMEOUT must be at least one second")


def request_is_authorized(headers: Any, expected_key: str) -> bool:
    authorization = str(headers.get("Authorization") or "")
    supplied = ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied:
        supplied = str(headers.get("x-api-key") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected_key)


def rewrite_request_body(raw_body: bytes, target_model: str) -> bytes:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be a UTF-8 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    payload["model"] = target_model
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _upstream_url(base_url: str, incoming_path: str) -> str:
    base = urllib.parse.urlsplit(base_url)
    incoming = urllib.parse.urlsplit(incoming_path)
    base_path = base.path.rstrip("/")
    request_path = incoming.path
    if base_path.endswith("/v1") and request_path.startswith("/v1/"):
        request_path = request_path[len("/v1") :]
    return urllib.parse.urlunsplit(
        (
            base.scheme,
            base.netloc,
            f"{base_path}{request_path}",
            incoming.query,
            "",
        )
    )


def build_handler(config: GatewayConfig) -> type[BaseHTTPRequestHandler]:
    class FlashGatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AntigravityFlashGateway/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.info("client=%s %s", self.client_address[0], fmt % args)

        def _json_response(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authenticate(self) -> bool:
            if request_is_authorized(self.headers, config.client_api_key):
                return True
            self._json_response(
                HTTPStatus.UNAUTHORIZED,
                {
                    "error": {
                        "message": "Invalid API key.",
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                    }
                },
            )
            return False

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                self._json_response(HTTPStatus.OK, {"status": "ok"})
                return
            if path != "/v1/models":
                self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authenticate():
                return
            self._json_response(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": config.target_model,
                            "object": "model",
                            "owned_by": "antigravity-flash-gateway",
                        }
                    ],
                },
            )

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path not in ALLOWED_POST_PATHS:
                self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authenticate():
                return
            if self.headers.get("Transfer-Encoding"):
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "chunked request bodies are not supported"},
                )
                return
            try:
                content_length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                content_length = -1
            if content_length < 1 or content_length > config.max_body_bytes:
                self._json_response(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "request body size is invalid"},
                )
                return
            try:
                body = rewrite_request_body(
                    self.rfile.read(content_length), config.target_model
                )
            except ValueError as exc:
                self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            upstream_headers = {
                "Authorization": f"Bearer {config.manager_api_key}",
                "Content-Type": "application/json",
            }
            for name in FORWARDED_REQUEST_HEADERS:
                if value := self.headers.get(name):
                    upstream_headers[name] = value
            request = urllib.request.Request(
                _upstream_url(config.manager_base_url, self.path),
                data=body,
                headers=upstream_headers,
                method="POST",
            )
            try:
                response = urllib.request.urlopen(
                    request, timeout=config.upstream_timeout_seconds
                )
            except urllib.error.HTTPError as exc:
                response = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                LOGGER.warning(
                    "Manager request failed: %s", getattr(exc, "reason", exc)
                )
                self._json_response(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": "Antigravity Manager is unavailable"},
                )
                return

            with response:
                self.send_response(response.status)
                for name, value in response.headers.items():
                    if name.lower() in FORWARDED_RESPONSE_HEADERS:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                while chunk := response.read(64 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            self.close_connection = True

    return FlashGatewayHandler


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host, port, config = load_config()
    server = GatewayServer((host, port), build_handler(config))
    LOGGER.info(
        "Listening on http://%s:%s; every request is pinned to model=%s",
        host,
        port,
        config.target_model,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
