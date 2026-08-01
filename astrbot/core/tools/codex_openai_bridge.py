from __future__ import annotations

import argparse
import asyncio
import contextlib
import heapq
import json
import logging
import os
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from quart import Quart, Response, jsonify, request

LOGGER = logging.getLogger(__name__)

_BACKGROUND_SESSION_PREFIXES = ("group-memory-compressor:",)


class _PriorityGate:
    def __init__(self, limit: int) -> None:
        self.limit = max(limit, 1)
        self.active = 0
        self.sequence = 0
        self.waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self.lock = asyncio.Lock()

    def _wake_locked(self) -> None:
        while self.active < self.limit and self.waiters:
            _, _, future = heapq.heappop(self.waiters)
            if future.cancelled():
                continue
            self.active += 1
            future.set_result(None)

    async def acquire(self, priority: int) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        async with self.lock:
            self.sequence += 1
            heapq.heappush(
                self.waiters,
                (priority, self.sequence, future),
            )
            self._wake_locked()
        try:
            await future
        except asyncio.CancelledError:
            async with self.lock:
                if future.done() and not future.cancelled():
                    self.active -= 1
                else:
                    future.cancel()
                self._wake_locked()
            raise

    async def release(self) -> None:
        async with self.lock:
            self.active -= 1
            self._wake_locked()

    @contextlib.asynccontextmanager
    async def slot(self, priority: int) -> Any:
        await self.acquire(priority)
        try:
            yield
        finally:
            await self.release()


def _request_priority(session_key: str | None) -> int:
    if session_key and session_key.startswith(_BACKGROUND_SESSION_PREFIXES):
        return 10
    return 0


@dataclass(frozen=True)
class BridgeConfig:
    host: str
    port: int
    default_model: str
    advertised_models: tuple[str, ...]
    codex_bin: str
    workdir: Path
    sandbox: str
    timeout_seconds: float
    auth_token: str | None
    stream_chunk_chars: int
    max_parallel_requests: int
    enable_search: bool
    codex_configs: tuple[str, ...]


@dataclass(frozen=True)
class BridgeAssistantResult:
    content: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()


class BridgeExecutionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "bridge_execution_failed",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class BridgeAppConfig(Protocol):
    default_model: str
    advertised_models: tuple[str, ...]
    workdir: Path
    auth_token: str | None
    stream_chunk_chars: int
    max_parallel_requests: int


PromptBuilder = Callable[..., str]
PromptRunner = Callable[..., Awaitable[str | BridgeAssistantResult]]


def _fallback_astrbot_temp_path() -> Path:
    root = Path(os.environ.get("ASTRBOT_ROOT", os.getcwd())).expanduser().resolve()
    return (root / "data" / "temp").resolve()


def _resolve_astrbot_temp_path() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    except Exception:
        return _fallback_astrbot_temp_path()

    try:
        return Path(get_astrbot_temp_path()).expanduser().resolve()
    except Exception:
        return _fallback_astrbot_temp_path()


def _bridge_temp_dir() -> Path:
    temp_dir = _resolve_astrbot_temp_path() / "codex_openai_bridge"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


_MAX_IMAGE_URL_CHARS = 200


def _describe_image_part(part: dict[str, Any]) -> str:
    """Name an image part without carrying its bytes into the prompt."""
    url = _extract_image_url(part)
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        mime = header[len("data:") :].split(";")[0].strip() or "image"
        approx_kb = max(1, len(payload) * 3 // 4 // 1024)
        return f"[Image omitted: {mime}, ~{approx_kb}KB]"
    if len(url) > _MAX_IMAGE_URL_CHARS:
        return f"[Image omitted: {url[:_MAX_IMAGE_URL_CHARS]}... ({len(url)} chars)]"
    return f"[Image omitted: {url}]"


def _extract_image_url(part: dict[str, Any]) -> str:
    image_data = part.get("image_url") or part.get("input_image") or {}
    if isinstance(image_data, dict):
        url = image_data.get("url") or image_data.get("image_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    if isinstance(image_data, str) and image_data.strip():
        return image_data.strip()
    return "inline-image"


def render_message_content(content: Any) -> str:
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        part_type = content.get("type")
        if part_type in {"text", "input_text"}:
            text = content.get("text", "")
            return text if isinstance(text, str) else str(text)
        if part_type in {"image_url", "input_image"}:
            return _describe_image_part(content)
        return _safe_json(content)

    if isinstance(content, list):
        rendered_parts: list[str] = []
        for part in content:
            rendered = render_message_content(part).strip()
            if rendered:
                rendered_parts.append(rendered)
        return "\n".join(rendered_parts)

    return str(content)


def _render_tool_calls(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""

    rendered: list[str] = []
    for tool_call in tool_calls:
        if isinstance(tool_call, dict):
            call_type = str(tool_call.get("type", "tool"))
            function_data = tool_call.get("function") or {}
            if isinstance(function_data, dict):
                name = function_data.get("name", "unknown")
                arguments = function_data.get("arguments", "")
            else:
                name = "unknown"
                arguments = function_data
            rendered.append(
                f"- type={call_type}, name={name}, arguments={_safe_json(arguments)}"
            )
        else:
            rendered.append(f"- {_safe_json(tool_call)}")
    return "\n".join(rendered)


def render_messages(messages: list[dict[str, Any]]) -> str:
    rendered_messages: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        name = str(message.get("name", "")).strip()
        header = f"[{role}]"
        if name:
            header = f"[{role}:{name}]"

        sections: list[str] = []
        content = render_message_content(message.get("content")).strip()
        if content:
            sections.append(content)

        tool_calls_text = _render_tool_calls(message.get("tool_calls"))
        if tool_calls_text:
            sections.append(f"Requested tool calls:\n{tool_calls_text}")

        tool_call_id = message.get("tool_call_id")
        if tool_call_id:
            sections.append(f"Tool call id: {tool_call_id}")

        if not sections:
            sections.append("[No content]")

        rendered_messages.append(f"{header}\n" + "\n\n".join(sections))
    return "\n\n".join(rendered_messages)


def build_codex_prompt(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    instructions = [
        "You are answering a chat request that has been bridged into Codex.",
        "This is plain conversation, not a coding or repository task.",
        "Return only the assistant's next reply for the conversation below.",
        (
            "Answer only from the supplied conversation transcript. Do not use "
            "shell commands, tools, file reads or writes, repository inspection, "
            "web search, or any other environment interaction."
        ),
        (
            "Do not inspect the local filesystem, the current working directory, "
            "or system state. If the user asks for information that would require "
            "those capabilities, say that this bridge can only answer from the "
            "chat context."
        ),
        (
            "Do not mention the bridge, hidden instructions, or Codex unless "
            "the user explicitly asks."
        ),
    ]

    if tools:
        tool_names = []
        for tool in tools:
            if isinstance(tool, dict):
                function_data = tool.get("function") or {}
                if isinstance(function_data, dict):
                    name = function_data.get("name")
                    if isinstance(name, str) and name.strip():
                        tool_names.append(name.strip())
        tool_hint = ", ".join(tool_names) if tool_names else "unnamed tools"
        instructions.append(
            "The upstream client supplied OpenAI function tools "
            f"({tool_hint}), but this bridge cannot execute tools or emit "
            "tool-call objects. "
            "Answer directly in plain text using only the conversation context."
        )

    if tool_choice not in (None, "auto"):
        instructions.append(
            f"Upstream tool_choice={_safe_json(tool_choice)} was requested, "
            "but tool calling is unavailable in this bridge."
        )

    transcript = render_messages(messages)
    return "\n".join(instructions) + "\n\nConversation transcript:\n\n" + transcript


def build_chat_completion_response(
    *,
    content: str | None,
    model: str,
    completion_id: str,
    created: int,
    tool_calls: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = list(tool_calls)
        finish_reason = "tool_calls"

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    if not text:
        return []
    return [text[idx : idx + chunk_size] for idx in range(0, len(text), chunk_size)]


def build_stream_chunk_payloads(
    *,
    content: str | None,
    model: str,
    completion_id: str,
    created: int,
    chunk_size: int,
    tool_calls: tuple[dict[str, Any], ...] = (),
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = [
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": None,
                }
            ],
        }
    ]

    for chunk in _chunk_text(content or "", chunk_size):
        payloads.append(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None,
                    }
                ],
            }
        )

    if tool_calls:
        payloads.append(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": list(tool_calls)},
                        "finish_reason": None,
                    }
                ],
            }
        )

    payloads.append(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
        }
    )
    return payloads


def build_codex_command(
    *,
    config: BridgeConfig,
    requested_model: str,
    output_path: Path,
) -> list[str]:
    command = [
        config.codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--sandbox",
        config.sandbox,
        "-C",
        str(config.workdir),
        "-o",
        str(output_path),
    ]

    if config.enable_search:
        command.append("--search")

    for config_item in config.codex_configs:
        command.extend(["-c", config_item])

    if requested_model:
        command.extend(["--model", requested_model])

    command.append("-")
    return command


def _tail_text(stdout_text: str, stderr_text: str) -> str:
    for raw_text in (stderr_text, stdout_text):
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if lines:
            return "\n".join(lines[-8:])
    return "Codex exited without a usable error message."


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    pid = process.pid
    if pid is None:
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)

    with contextlib.suppress(Exception):
        await process.wait()


async def run_codex_prompt(
    *,
    prompt: str,
    requested_model: str,
    config: BridgeConfig,
    **_: Any,
) -> str:
    output_path = _bridge_temp_dir() / f"{uuid.uuid4().hex}.txt"
    command = build_codex_command(
        config=config,
        requested_model=requested_model,
        output_path=output_path,
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise BridgeExecutionError(
            f"Codex CLI was not found: {config.codex_bin}",
            status_code=500,
            code="codex_not_found",
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")),
            timeout=config.timeout_seconds,
        )
    except asyncio.CancelledError:
        await _kill_process_group(process)
        raise
    except TimeoutError as exc:
        await _kill_process_group(process)
        raise BridgeExecutionError(
            f"Codex request timed out after {config.timeout_seconds:.0f} seconds.",
            status_code=504,
            code="codex_timeout",
        ) from exc

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")

    try:
        output_text = output_path.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError:
        output_text = ""
    finally:
        with contextlib.suppress(FileNotFoundError):
            output_path.unlink()

    if process.returncode != 0:
        raise BridgeExecutionError(_tail_text(stdout_text, stderr_text))

    if not output_text.strip():
        raise BridgeExecutionError(
            "Codex completed but did not produce a final assistant message."
        )

    return output_text


def _build_error_response(
    message: str,
    *,
    status_code: int,
    code: str,
) -> tuple[Response, int]:
    payload = {
        "error": {
            "message": message,
            "type": "invalid_request_error" if status_code < 500 else "api_error",
            "code": code,
        }
    }
    return jsonify(payload), status_code


def _require_auth(config: BridgeAppConfig) -> tuple[Response, int] | None:
    if not config.auth_token:
        return None

    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if token == config.auth_token:
        return None

    return _build_error_response(
        "Unauthorized. Supply the configured bearer token.",
        status_code=401,
        code="unauthorized",
    )


def _model_payload(model_id: str, *, owned_by: str = "codex-bridge") -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": owned_by,
    }


def create_openai_compatible_app(
    config: BridgeAppConfig,
    *,
    bridge_name: str,
    backend_name: str,
    model_owner: str,
    prompt_builder: PromptBuilder,
    prompt_runner: PromptRunner,
    gate_bypass: Callable[..., bool] | None = None,
) -> Quart:
    app = Quart(__name__)
    execution_gate = _PriorityGate(config.max_parallel_requests)

    @app.get("/")
    async def index() -> Response:
        return jsonify(
            {
                "name": bridge_name,
                "models": list(config.advertised_models),
                "workdir": str(config.workdir),
            }
        )

    @app.get("/healthz")
    async def healthz() -> Response:
        return jsonify({"ok": True})

    @app.get("/models")
    @app.get("/v1/models")
    async def list_models() -> Response | tuple[Response, int]:
        if auth_error := _require_auth(config):
            return auth_error
        return jsonify(
            {
                "object": "list",
                "data": [
                    _model_payload(model_id, owned_by=model_owner)
                    for model_id in config.advertised_models
                ],
            }
        )

    @app.get("/models/<path:model_id>")
    @app.get("/v1/models/<path:model_id>")
    async def get_model(model_id: str) -> Response | tuple[Response, int]:
        if auth_error := _require_auth(config):
            return auth_error
        return jsonify(_model_payload(model_id, owned_by=model_owner))

    @app.post("/chat/completions")
    @app.post("/v1/chat/completions")
    async def chat_completions() -> Response | tuple[Response, int]:
        if auth_error := _require_auth(config):
            return auth_error

        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _build_error_response(
                "Request body must be a JSON object.",
                status_code=400,
                code="invalid_json",
            )

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return _build_error_response(
                "`messages` must be a non-empty array.",
                status_code=400,
                code="missing_messages",
            )

        normalized_messages = [
            message for message in messages if isinstance(message, dict)
        ]
        if not normalized_messages:
            return _build_error_response(
                "No valid message objects were supplied.",
                status_code=400,
                code="invalid_messages",
            )

        requested_model = str(payload.get("model") or config.default_model)
        session_key = str(payload.get("user") or "").strip() or None
        priority = _request_priority(session_key)
        request_id = uuid.uuid4().hex[:12]
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else None
        tool_choice = payload.get("tool_choice")
        prompt = prompt_builder(
            normalized_messages,
            tools=tools,
            tool_choice=tool_choice,
        )

        LOGGER.info(
            "Forwarding chat completion to %s. model=%s messages=%s stream=%s request_id=%s",
            backend_name,
            requested_model,
            len(normalized_messages),
            bool(payload.get("stream")),
            request_id,
        )

        queued_at = time.monotonic()
        wait_seconds = 0.0
        started_at: float | None = None
        # The gate serialises access to the CLI backend. A request that will not
        # touch that backend must not hold the only slot, or one slow request of
        # that kind makes the bridge unresponsive for everybody else.
        bypass_gate = False
        if gate_bypass is not None:
            try:
                bypass_gate = bool(
                    gate_bypass(messages=normalized_messages, session_key=session_key)
                )
            except Exception:
                LOGGER.exception("gate_bypass check failed; falling back to the gate")
        if bypass_gate:
            LOGGER.info(
                "Bypassing the execution gate; this route does not use %s. request_id=%s",
                backend_name,
                request_id,
            )
        gate = (
            contextlib.nullcontext() if bypass_gate else execution_gate.slot(priority)
        )
        try:
            async with gate:
                wait_seconds = time.monotonic() - queued_at
                started_at = time.monotonic()
                runner_result = await prompt_runner(
                    prompt=prompt,
                    requested_model=requested_model,
                    config=config,
                    session_key=session_key,
                    messages=normalized_messages,
                    tools=tools,
                    tool_choice=tool_choice,
                )
        except BridgeExecutionError as exc:
            run_seconds = (
                time.monotonic() - started_at if started_at is not None else 0.0
            )
            LOGGER.warning(
                "Failed chat completion via %s. model=%s messages=%s wait_s=%.2f run_s=%.2f request_id=%s code=%s error=%s",
                backend_name,
                requested_model,
                len(normalized_messages),
                wait_seconds,
                run_seconds,
                request_id,
                exc.code,
                exc,
            )
            return _build_error_response(
                str(exc),
                status_code=exc.status_code,
                code=exc.code,
            )

        if isinstance(runner_result, BridgeAssistantResult):
            content = runner_result.content
            tool_calls = runner_result.tool_calls
        else:
            content = runner_result
            tool_calls = ()

        LOGGER.info(
            "Completed chat completion via %s. model=%s messages=%s wait_s=%.2f run_s=%.2f request_id=%s",
            backend_name,
            requested_model,
            len(normalized_messages),
            wait_seconds,
            time.monotonic() - started_at,
            request_id,
        )

        completion_id = f"chatcmpl-{request_id}-{uuid.uuid4().hex}"
        created = int(time.time())

        if payload.get("stream"):

            async def stream_events() -> Any:
                for item in build_stream_chunk_payloads(
                    content=content,
                    model=requested_model,
                    completion_id=completion_id,
                    created=created,
                    chunk_size=config.stream_chunk_chars,
                    tool_calls=tool_calls,
                ):
                    yield f"data: {_safe_json(item)}\n\n"
                yield "data: [DONE]\n\n"

            return Response(
                stream_events(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return jsonify(
            build_chat_completion_response(
                content=content,
                model=requested_model,
                completion_id=completion_id,
                created=created,
                tool_calls=tool_calls,
            )
        )

    return app


def create_app(config: BridgeConfig) -> Quart:
    return create_openai_compatible_app(
        config,
        bridge_name="codex-openai-bridge",
        backend_name="Codex",
        model_owner="codex-bridge",
        prompt_builder=build_codex_prompt,
        prompt_runner=run_codex_prompt,
    )


def parse_args(argv: list[str] | None = None) -> BridgeConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Expose a local OpenAI-compatible chat endpoint backed by Codex CLI."
        )
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1"),
        help="Bind address for the local bridge server.",
    )
    parser.add_argument(
        "--port",
        default=int(os.environ.get("CODEX_BRIDGE_PORT", "8787")),
        type=int,
        help="Bind port for the local bridge server.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODEX_BRIDGE_MODEL", "gpt-5.4"),
        help=(
            "Default model name to advertise and to use when the request omits `model`."
        ),
    )
    parser.add_argument(
        "--advertise-model",
        action="append",
        default=[],
        help="Additional model IDs to expose from /v1/models.",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("CODEX_BRIDGE_CODEX_BIN", "codex"),
        help="Codex CLI binary to invoke.",
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("CODEX_BRIDGE_WORKDIR", os.getcwd()),
        help="Working directory passed to `codex exec -C`.",
    )
    parser.add_argument(
        "--sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default=os.environ.get("CODEX_BRIDGE_SANDBOX", "read-only"),
        help="Sandbox mode used for Codex executions.",
    )
    parser.add_argument(
        "--timeout",
        default=float(os.environ.get("CODEX_BRIDGE_TIMEOUT", "90")),
        type=float,
        help="Maximum time to wait for a Codex response, in seconds.",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("CODEX_BRIDGE_AUTH_TOKEN") or None,
        help="Optional bearer token required by the bridge.",
    )
    parser.add_argument(
        "--stream-chunk-chars",
        default=int(os.environ.get("CODEX_BRIDGE_STREAM_CHUNK_CHARS", "64")),
        type=int,
        help="Pseudo-stream chunk size in characters for stream=true requests.",
    )
    parser.add_argument(
        "--max-parallel-requests",
        default=int(os.environ.get("CODEX_BRIDGE_MAX_PARALLEL", "1")),
        type=int,
        help="Maximum number of concurrent Codex executions.",
    )
    parser.add_argument(
        "--enable-search",
        action="store_true",
        default=os.environ.get("CODEX_BRIDGE_ENABLE_SEARCH", "").lower() == "true",
        help="Pass --search to Codex for every request.",
    )
    parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        help="Repeatable `key=value` config overrides forwarded as `codex exec -c`.",
    )

    args = parser.parse_args(argv)

    advertised_models = tuple(
        dict.fromkeys([args.model, *[model for model in args.advertise_model if model]])
    )
    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    return BridgeConfig(
        host=args.host,
        port=args.port,
        default_model=args.model,
        advertised_models=advertised_models,
        codex_bin=args.codex_bin,
        workdir=workdir,
        sandbox=args.sandbox,
        timeout_seconds=args.timeout,
        auth_token=args.auth_token,
        stream_chunk_chars=max(args.stream_chunk_chars, 1),
        max_parallel_requests=max(args.max_parallel_requests, 1),
        enable_search=args.enable_search,
        codex_configs=tuple(args.codex_config),
    )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = parse_args(argv)
    app = create_app(config)
    LOGGER.info(
        "Starting Codex bridge on http://%s:%s (model=%s, workdir=%s)",
        config.host,
        config.port,
        config.default_model,
        config.workdir,
    )
    app.run(host=config.host, port=config.port)


if __name__ == "__main__":
    main()
