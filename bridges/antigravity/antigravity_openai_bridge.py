from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import importlib.util
import json
import logging
import os
import queue
import re
import select
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quart import Quart

LOGGER = logging.getLogger(__name__)

_PROTOCOL_BEGIN = "<<<AGY_RESPONSE>>>"
_PROTOCOL_END = "<<<END_AGY_RESPONSE>>>"
_PERSISTENT_READ_CHARS = 2048
_TRANSCRIPT_POLL_SECONDS = 1.0
_TRANSCRIPT_RECENT_GRACE_SECONDS = 5.0
_TRANSCRIPT_CANDIDATE_LIMIT = 16
_DEFAULT_OPUS_MODEL = "claude-opus-4-6-thinking"
_DEFAULT_GPT_MODEL = "chatgpt-web"
_CHATGPT_WEB_ENDPOINT = os.environ.get(
    "CHATGPT_WEB_ENDPOINT", "http://127.0.0.1:8766/v1/chat/completions"
)
_MANAGER_SYSTEM_POLICY = """This request is routed through Antigravity Manager.
Follow the supplied conversation and tool definitions. Do not use a web-search
capability unless the latest user message explicitly asks to search, browse,
look something up, or verify it online. Do not call an Exa tool unless the
latest user message explicitly asks to use Exa. Merely mentioning a tool,
asking not to use it, or deciding that it would help is not permission.
Never claim that a tool or search succeeded unless its current-turn result was
actually returned to you."""

_BACKGROUND_SESSION_PREFIXES = ("group-memory-compressor:",)
_GPT_NEGATIVE_PATTERN = re.compile(
    r"(?:不要|别|不必|无需|禁止)\s*(?:使用|用|调用|切到|切换到)?\s*"
    r"(?:chat\s*gpt|gpt)|(?:说过|提到|讨论|谈到).{0,20}"
    r"(?:使用|用)\s*(?:chat\s*gpt|gpt)|"
    r"\b(?:do\s+not|don't|without)\s+use\s+(?:chat\s*gpt|gpt)\b",
    re.IGNORECASE,
)
_GPT_POSITIVE_PATTERNS = [
    re.compile(
        r"(?:请|麻烦)?\s*(?:使用|用|调用|改用|切到|切换到|换成|让)"
        r"(?:一下)?\s*(?:那个)?\s*(?:chat\s*)?gpt",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:use|ask|switch\s+to|route\s+to)\s+(?:chat\s*)?gpt\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:chat\s*)?gpt[\s,\u3002,:：!？!]", re.IGNORECASE),
]

_OPUS_NEGATIVE_PATTERN = re.compile(
    r"(?:不要|别|不必|无需|禁止)\s*(?:使用|用|调用|切到|切换到)?\s*"
    r"(?:claude\s*)?opus|(?:说过|提到|讨论|谈到).{0,20}"
    r"(?:使用|用)\s*(?:claude\s*)?opus|"
    r"\b(?:do\s+not|don't|without)\s+use\s+(?:claude\s+)?opus\b",
    re.IGNORECASE,
)
_OPUS_POSITIVE_PATTERNS = (
    re.compile(
        r"(?:请|麻烦)?\s*(?:使用|用|调用|改用|切到|切换到|换成|让)\s*"
        r"(?:一下)?\s*(?:那个)?\s*(?:claude\s*)?opus"
        r"(?:\s*4(?:[.\-\s]?6))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:use|ask|switch\s+to|route\s+to)\s+(?:claude\s+)?opus\b",
        re.IGNORECASE,
    ),
)


def _load_shared_bridge_module() -> Any:
    module_name = "astrbot_codex_openai_bridge_shared"
    if loaded_module := sys.modules.get(module_name):
        return loaded_module

    module_path = Path(__file__).with_name("codex_openai_bridge.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load shared bridge module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_SHARED_BRIDGE = _load_shared_bridge_module()
BridgeExecutionError = _SHARED_BRIDGE.BridgeExecutionError
BridgeAssistantResult = _SHARED_BRIDGE.BridgeAssistantResult
create_openai_compatible_app = _SHARED_BRIDGE.create_openai_compatible_app
render_messages = _SHARED_BRIDGE.render_messages


@dataclass(frozen=True)
class BridgeConfig:
    host: str
    port: int
    default_model: str
    advertised_models: tuple[str, ...]
    agy_bin: str
    agy_model: str | None
    opus_model: str | None
    effort: str | None
    workdir: Path
    timeout_seconds: float
    print_timeout_seconds: float
    auth_token: str | None
    stream_chunk_chars: int
    max_parallel_requests: int
    sandbox: bool
    reuse_conversations: bool
    session_max_turns: int
    session_idle_seconds: float
    session_state_file: Path
    persistent_tui: bool
    persistent_session_limit: int
    persistent_columns: int
    persistent_rows: int
    persistent_write_timeout_seconds: float
    persistent_submit_timeout_seconds: float
    persistent_progress_timeout_seconds: float
    persistent_buffer_screens: int
    manager_base_url: str | None = None
    manager_api_key: str | None = None
    manager_model: str | None = None
    manager_timeout_seconds: float = 300


@dataclass
class ConversationSession:
    conversation_id: str
    turn_count: int
    last_used_at: float
    assistant_marker: dict[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ConversationSession | None:
        conversation_id = str(value.get("conversation_id") or "").strip()
        if not conversation_id:
            return None
        return cls(
            conversation_id=conversation_id,
            turn_count=max(int(value.get("turn_count") or 0), 0),
            last_used_at=float(value.get("last_used_at") or 0),
            assistant_marker=(
                value.get("assistant_marker")
                if isinstance(value.get("assistant_marker"), dict)
                else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "turn_count": self.turn_count,
            "last_used_at": self.last_used_at,
            "assistant_marker": self.assistant_marker,
        }


class ConversationRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sessions: dict[str, ConversationSession] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(payload, dict):
            return
        for key, value in payload.items():
            if isinstance(key, str) and isinstance(value, dict):
                session = ConversationSession.from_dict(value)
                if session:
                    self.sessions[key] = session

    def get(self, key: str) -> ConversationSession | None:
        return self.sessions.get(key)

    def put(self, key: str, session: ConversationSession) -> None:
        self.sessions[key] = session
        self._save()

    def remove(self, key: str) -> None:
        if self.sessions.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(
                {key: value.to_dict() for key, value in self.sessions.items()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp_path.replace(self.path)


_REGISTRIES: dict[Path, ConversationRegistry] = {}


def _registry(config: BridgeConfig) -> ConversationRegistry:
    path = config.session_state_file.resolve()
    if path not in _REGISTRIES:
        _REGISTRIES[path] = ConversationRegistry(path)
    return _REGISTRIES[path]


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _message_content_text(message.get("content"))
    return ""


def _explicitly_requests_opus(messages: list[dict[str, Any]]) -> bool:
    latest_user_text = _latest_user_text(messages)
    if not latest_user_text or _OPUS_NEGATIVE_PATTERN.search(latest_user_text):
        return False
    return any(pattern.search(latest_user_text) for pattern in _OPUS_POSITIVE_PATTERNS)


def _explicitly_requests_gpt(messages: list[dict[str, Any]]) -> bool:
    latest_user_text = _latest_user_text(messages)
    if not latest_user_text or _GPT_NEGATIVE_PATTERN.search(latest_user_text):
        return False
    return any(pattern.search(latest_user_text) for pattern in _GPT_POSITIVE_PATTERNS)


def _select_model_route(
    messages: list[dict[str, Any]],
    *,
    config: BridgeConfig,
    session_key: str | None,
) -> tuple[str, str | None]:
    is_background = bool(
        session_key
        and any(
            session_key.startswith(prefix) for prefix in _BACKGROUND_SESSION_PREFIXES
        )
    )
    if config.opus_model and not is_background and _explicitly_requests_opus(messages):
        return "opus", config.opus_model
    if not is_background and _explicitly_requests_gpt(messages):
        return "gpt", _DEFAULT_GPT_MODEL
    return "default", config.agy_model


async def _chatgpt_web_completion(
    messages: list[dict[str, Any]],
    *,
    timeout_s: float = float(os.environ.get("CHATGPT_WEB_TIMEOUT_S", "570")),
) -> BridgeAssistantResult:
    import urllib.request

    body = json.dumps({"model": _DEFAULT_GPT_MODEL, "messages": messages}).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        _CHATGPT_WEB_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _post() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    LOGGER.info(
        "Routing chat completion to ChatGPT Web bridge. endpoint=%s",
        _CHATGPT_WEB_ENDPOINT,
    )
    payload = await asyncio.to_thread(_post)
    message = payload["choices"][0]["message"]
    raw_content = message.get("content")
    content = str(raw_content) if raw_content is not None else None
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        LOGGER.info(
            "ChatGPT Web bridge returned %s tool call(s); forwarding them.",
            len(tool_calls),
        )
        return BridgeAssistantResult(content=content, tool_calls=tuple(tool_calls))
    return BridgeAssistantResult(content=content or "")


def _manager_chat_completions_url(base_url: str) -> str:
    raw_url = base_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BridgeExecutionError(
            "Antigravity Manager Base URL must be an absolute HTTP(S) URL.",
            status_code=500,
            code="antigravity_manager_url_invalid",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeExecutionError(
            "Antigravity Manager Base URL cannot contain credentials, a query, or a fragment.",
            status_code=500,
            code="antigravity_manager_url_invalid",
        )
    if parsed.path.rstrip("/").endswith("/v1/chat/completions"):
        return raw_url
    if parsed.path.rstrip("/").endswith("/v1"):
        return f"{raw_url}/chat/completions"
    return f"{raw_url}/v1/chat/completions"


def _manager_error_detail(raw_body: bytes) -> str | None:
    text = raw_body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    if not isinstance(payload, dict):
        return text[:500]
    error = payload.get("error")
    if isinstance(error, dict):
        detail = error.get("message") or error.get("code")
    else:
        detail = error or payload.get("message")
    return str(detail)[:500] if detail else None


def _manager_message_content(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts = [
            str(part.get("text"))
            for part in value
            if isinstance(part, dict)
            and part.get("type") in {"text", "output_text"}
            and part.get("text") is not None
        ]
        if text_parts:
            return "\n".join(text_parts)
    return json.dumps(value, ensure_ascii=False)


async def _antigravity_manager_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    model: str,
    config: BridgeConfig,
) -> BridgeAssistantResult:
    if not config.manager_base_url or not config.manager_api_key:
        raise BridgeExecutionError(
            "Antigravity Manager backend is missing its Base URL or API Key.",
            status_code=500,
            code="antigravity_manager_config_missing",
        )

    endpoint = _manager_chat_completions_url(config.manager_base_url)
    manager_messages = [
        {"role": "system", "content": _MANAGER_SYSTEM_POLICY},
        *messages,
    ]
    payload: dict[str, Any] = {
        "model": model,
        "messages": manager_messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.manager_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def _post() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(
                request,
                timeout=config.manager_timeout_seconds,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _manager_error_detail(exc.read())
            message = f"Antigravity Manager returned HTTP {exc.code}."
            if detail:
                message = f"{message} {detail}"
            raise BridgeExecutionError(
                message,
                status_code=502,
                code="antigravity_manager_http_error",
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise BridgeExecutionError(
                f"Could not reach Antigravity Manager: {reason}",
                status_code=502,
                code="antigravity_manager_unreachable",
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BridgeExecutionError(
                "Antigravity Manager returned an invalid JSON response.",
                status_code=502,
                code="antigravity_manager_response_invalid",
            ) from exc

    LOGGER.info(
        "Routing completion to Antigravity Manager. endpoint=%s model=%s",
        endpoint,
        model,
    )
    response_payload = await asyncio.to_thread(_post)
    try:
        message = response_payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BridgeExecutionError(
            "Antigravity Manager response did not contain an assistant message.",
            status_code=502,
            code="antigravity_manager_response_invalid",
        ) from exc
    if not isinstance(message, dict):
        raise BridgeExecutionError(
            "Antigravity Manager returned an invalid assistant message.",
            status_code=502,
            code="antigravity_manager_response_invalid",
        )
    raw_tool_calls = message.get("tool_calls") or []
    tool_calls = tuple(item for item in raw_tool_calls if isinstance(item, dict))
    content = _manager_message_content(message.get("content"))
    if content is None and not tool_calls:
        raise BridgeExecutionError(
            "Antigravity Manager returned an empty assistant message.",
            status_code=502,
            code="antigravity_manager_response_invalid",
        )
    return BridgeAssistantResult(content=content, tool_calls=tool_calls)


def _model_scoped_session_key(session_key: str | None, route: str) -> str | None:
    if not session_key or route == "default":
        return session_key
    return f"{session_key}:model-route:{route}"


def build_antigravity_prompt(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    instructions = [
        "You are answering a plain chat request bridged into Antigravity CLI.",
        "Return the assistant's next reply using the strict YAML envelope below.",
        (
            "Use only the supplied conversation transcript, results returned by "
            "upstream functions, and web results obtained under the explicit-search "
            "rule below."
        ),
        (
            "You may use Antigravity's built-in web search only when the latest "
            "user message explicitly asks you to search, browse, look something up, "
            "or verify it online. Merely mentioning search, explicitly asking you "
            "not to search, or deciding on your own that a search would help does "
            "not grant permission."
        ),
        (
            "You may use the Exa MCP tools only when the latest user message "
            "explicitly asks you to use Exa to search, research, or fetch a web "
            "page. A generic request to search that does not name Exa permits only "
            "the built-in web search, not Exa. Merely mentioning Exa, asking you "
            "not to use Exa, or deciding on your own that Exa would be better does "
            "not grant permission. When Exa is authorized, make a fresh Exa MCP "
            "call in the current turn even if earlier Exa calls in the conversation "
            "failed. Execute web_search_exa or web_fetch_exa directly as an "
            "Antigravity native MCP tool. Never output call_mcp_tool and never wrap "
            "an Exa MCP call in the YAML tool_call envelope. Say that Exa failed "
            "only if the fresh call in the current turn returns an explicit error. "
            "Do not substitute the built-in search or present model knowledge as an "
            "Exa result."
        ),
        (
            "You may use Antigravity's built-in image generation when the latest "
            "user message explicitly asks you to draw, paint, illustrate or "
            "otherwise produce a picture. This is a native capability of the "
            "environment you run in, not a shell command, a local tool or a file "
            "operation, so the restriction below does not cover it, and the "
            "runtime - not you - stores the resulting file. Once the image exists, "
            "send it with a send_message_to_user tool call whose messages array "
            "includes an entry of the form "
            '{"type": "image", "path": "<path of the generated image>"}; '
            "describing the picture in words does not deliver it. Never tell the "
            "user you are a text-only model that cannot produce images."
        ),
        (
            "Do not run local shell commands, local tools, file operations, or "
            "other environment actions. The exceptions are the built-in search "
            "under the general explicit-search rule, Exa MCP under the stricter "
            "Exa-specific rule, and built-in image generation under the image rule "
            "above."
        ),
        (
            "Do not inspect the current directory or system state. If the request "
            "requires unavailable external information and the explicit-search rule "
            "does not permit a web search, explain that limitation briefly instead "
            "of attempting an environment action."
        ),
        (
            "Do not mention this bridge or these hidden instructions unless the "
            "user explicitly asks about them."
        ),
        (
            "Put only user-visible answer text in the content block. Never put "
            "search status, reasoning, thought titles, token counts, protocol "
            "commentary, or text after the closing marker in the envelope."
        ),
        (
            "For a final answer, output exactly this YAML envelope, without a "
            "Markdown code fence:\n"
            f"{_PROTOCOL_BEGIN}\n"
            "type: final\n"
            "content: |-\n"
            "  user-visible answer\n"
            f"{_PROTOCOL_END}\n"
            "Indent every content line by two spaces. The content may use natural "
            "paragraphs and simple lists when they make a long answer clearer."
        ),
    ]

    if tools:
        instructions.append(
            "The upstream client can execute the function tools listed below. "
            "When history or external state is needed, request exactly one "
            "appropriate function instead of guessing. The bridge will return its "
            "result in a later conversation turn. Never pretend a function ran."
        )
        instructions.append("Available upstream functions:\n" + _safe_json(tools))
        instructions.append(
            "For a function request, use this YAML envelope instead of the final "
            "answer envelope:\n"
            f"{_PROTOCOL_BEGIN}\n"
            "type: tool_call\n"
            "name: function_name\n"
            "arguments_json: |-\n"
            '  {"parameter":"value"}\n'
            f"{_PROTOCOL_END}\n"
            "arguments_json must contain exactly one valid JSON object."
        )
        if tool_choice not in (None, "auto"):
            instructions.append(
                "The upstream tool choice is "
                f"{_safe_json(tool_choice)}; obey it when selecting the response type."
            )

    elif tool_choice not in (None, "auto"):
        instructions.append(
            f"Upstream tool_choice={_safe_json(tool_choice)} was requested, but no "
            "function definitions were supplied."
        )

    return (
        "\n".join(instructions)
        + "\n\nConversation transcript:\n\n"
        + render_messages(messages)
    )


def build_antigravity_command(
    *,
    config: BridgeConfig,
    prompt: str,
    conversation_id: str | None = None,
    agy_model: str | None = None,
) -> list[str]:
    command = [
        config.agy_bin,
        "-p",
        prompt,
        "--output-format",
        "text",
        "--print-timeout",
        f"{max(round(config.print_timeout_seconds), 1)}s",
    ]

    if config.sandbox:
        command.append("--sandbox")
    selected_model = agy_model or config.agy_model
    if selected_model:
        command.extend(["--model", selected_model])
    if config.effort:
        command.extend(["--effort", config.effort])
    if conversation_id:
        command.extend(["--conversation", conversation_id])

    return command


def build_antigravity_interactive_command(
    *,
    config: BridgeConfig,
    prompt: str,
    conversation_id: str | None = None,
    agy_model: str | None = None,
) -> list[str]:
    command = [
        config.agy_bin,
        "--prompt-interactive",
        prompt,
    ]

    if config.sandbox:
        command.append("--sandbox")
    selected_model = agy_model or config.agy_model
    if selected_model:
        command.extend(["--model", selected_model])
    if config.effort:
        command.extend(["--effort", config.effort])
    if conversation_id:
        command.extend(["--conversation", conversation_id])

    return command


def _is_terminal_separator(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 20 and set(stripped) == {"─"}


def _extract_protocol_json(lines: list[str]) -> str | None:
    for start_index in range(len(lines) - 1, -1, -1):
        if not lines[start_index].lstrip().startswith("{"):
            continue
        for end_index in range(len(lines), start_index, -1):
            candidate = "".join(lines[start_index:end_index]).strip()
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("type") or "").lower() not in {
                "final",
                "tool_call",
            }:
                continue
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return None


def _yaml_value(lines: list[str], key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.*?)\s*$")
    for line in lines:
        if match := pattern.match(line):
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                return value[1:-1]
            return value
    return None


def _soft_wrap_separator(previous: str, continuation: str) -> str:
    if not previous or not continuation:
        return ""
    previous_char = previous[-1]
    continuation_char = continuation[0]
    if previous_char.isspace() or continuation_char.isspace():
        return ""
    if previous_char.isascii() and previous_char.isalnum():
        return " "
    if continuation_char.isascii() and continuation_char.isalnum():
        return " "
    return ""


def _yaml_block(lines: list[str], key: str) -> str | None:
    header_pattern = re.compile(rf"^{re.escape(key)}:\s*\|[-+]?\s*$")
    header_index = next(
        (index for index, line in enumerate(lines) if header_pattern.match(line)),
        None,
    )
    if header_index is None:
        return None

    content_lines: list[str] = []
    for line in lines[header_index + 1 :]:
        if not line:
            content_lines.append("")
            continue
        if line.startswith("  "):
            content_lines.append(line[2:])
            continue
        if not content_lines:
            content_lines.append(line)
            continue
        separator = _soft_wrap_separator(content_lines[-1], line)
        content_lines[-1] += separator + line

    return "\n".join(content_lines).strip()


def _normalize_visible_content(content: str) -> str:
    normalized_lines: list[str] = []
    in_code_fence = False
    for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence
            normalized_lines.append(line)
            continue
        if not in_code_fence:
            line = re.sub(r"^(\s*)#{1,6}\s+", r"\1", line)
            line = re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"\1", line)
        normalized_lines.append(line.rstrip())

    normalized = "\n".join(normalized_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


def _extract_protocol_yaml(lines: list[str]) -> str | None:
    begin_indexes = [
        index for index, line in enumerate(lines) if line.strip() == _PROTOCOL_BEGIN
    ]
    for begin_index in reversed(begin_indexes):
        end_index = next(
            (
                index
                for index in range(begin_index + 1, len(lines))
                if lines[index].strip() == _PROTOCOL_END
            ),
            None,
        )
        if end_index is None:
            continue

        begin_line = lines[begin_index]
        indentation = begin_line[: len(begin_line) - len(begin_line.lstrip())]
        body_lines = [
            line[len(indentation) :] if line.startswith(indentation) else line
            for line in lines[begin_index + 1 : end_index]
        ]
        response_type = (_yaml_value(body_lines, "type") or "").lower()
        if response_type == "final":
            content = _yaml_block(body_lines, "content")
            if content is None:
                continue
            payload = {
                "type": "final",
                "content": _normalize_visible_content(content),
            }
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        if response_type == "tool_call":
            name = _yaml_value(body_lines, "name")
            arguments_json = _yaml_block(body_lines, "arguments_json")
            if not name or arguments_json is None:
                continue
            try:
                arguments = json.loads(arguments_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(arguments, dict):
                continue
            payload = {
                "type": "tool_call",
                "name": name,
                "arguments": arguments,
            }
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return None


def _extract_tui_response(lines: list[str], marker: str) -> str:
    marker_indexes = [index for index, line in enumerate(lines) if marker in line]
    if not marker_indexes:
        return ""

    response_lines: list[str] = []
    for line in lines[marker_indexes[0] + 1 :]:
        if _is_terminal_separator(line):
            break
        response_lines.append(line)

    while response_lines and not response_lines[0].strip():
        response_lines.pop(0)
    while response_lines and not response_lines[-1].strip():
        response_lines.pop()
    normalized_lines = [
        line[2:] if line.startswith("  ") else line for line in response_lines
    ]
    if protocol_yaml := _extract_protocol_yaml(normalized_lines):
        return protocol_yaml
    if protocol_json := _extract_protocol_json(normalized_lines):
        return protocol_json
    return "\n".join(normalized_lines).strip()


def _render_tui_screen(screen: Any) -> list[str]:
    lines: list[str] = []
    for y_position in range(screen.lines):
        row = screen.buffer[y_position]
        characters: list[str] = []
        for x_position in range(screen.columns):
            cell = row[x_position]
            data = getattr(cell, "data", None)
            if data is None:
                with contextlib.suppress(IndexError, TypeError):
                    data = cell[0]
            characters.append(" " if data is None else data)
        lines.append("".join(characters).rstrip())
    return lines


def _marked_tui_prompt(prompt: str, marker: str) -> str:
    return (
        f"{prompt}\n\n"
        "The final line below is an internal transport marker. "
        "Do not quote or mention it in your response.\n"
        f"{marker}"
    )


@dataclass
class PersistentTuiRequest:
    marker: str
    marked_prompt: str
    needs_write: bool
    future: concurrent.futures.Future[str] = field(
        default_factory=concurrent.futures.Future
    )
    started_at: float = 0.0
    submitted_at: float = 0.0
    last_progress_at: float = 0.0
    submit_confirmed: bool = False
    marker_seen: bool = False
    protocol_lines: list[str] = field(default_factory=list)
    terminal_protocol: str = ""
    snapshots: deque[tuple[str, ...]] = field(default_factory=deque)
    submitted_wall_at: float = field(default_factory=time.time)
    last_transcript_poll_at: float = 0.0
    resolved_conversation_id: str | None = None


@dataclass
class PersistentTuiSession:
    child: Any
    screen: Any
    stream: Any
    turn_count: int
    last_used_at: float
    conversation_id: str | None
    known_conversation_ids: set[str]
    requests: queue.Queue[PersistentTuiRequest]
    stop_event: threading.Event
    closed_event: threading.Event
    screen_snapshots: deque[tuple[str, ...]]
    initial_request: PersistentTuiRequest | None = None
    worker: threading.Thread | None = None


def _merge_protocol_view(previous: list[str], current: list[str]) -> list[str]:
    if not previous:
        return current
    if not current:
        return previous

    if current[0].strip() == _PROTOCOL_BEGIN:
        common = 0
        for old_line, new_line in zip(previous, current, strict=False):
            if old_line != new_line:
                break
            common += 1
        if common >= min(3, len(previous), len(current)):
            return current if len(current) >= len(previous) else previous

    maximum = min(len(previous), len(current))
    for overlap in range(maximum, 0, -1):
        for start in range(0, len(current) - overlap + 1):
            if previous[-overlap:] == current[start : start + overlap]:
                return [*previous, *current[start + overlap :]]

    return previous


def _capture_tui_response(
    request: PersistentTuiRequest,
    lines: list[str],
) -> str:
    snapshot = tuple(lines)
    if not request.snapshots or request.snapshots[-1] != snapshot:
        request.snapshots.append(snapshot)

    marker_indexes = [
        index for index, line in enumerate(lines) if request.marker in line
    ]
    if marker_indexes:
        request.marker_seen = True
        response_lines = lines[marker_indexes[-1] + 1 :]
        begin_index = next(
            (
                index
                for index, line in enumerate(response_lines)
                if line.strip() == _PROTOCOL_BEGIN
            ),
            None,
        )
        if begin_index is not None:
            request.protocol_lines = _merge_protocol_view(
                request.protocol_lines,
                response_lines[begin_index:],
            )
    elif request.protocol_lines:
        begin_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip() == _PROTOCOL_BEGIN
            ),
            None,
        )
        request.protocol_lines = _merge_protocol_view(
            request.protocol_lines,
            lines[begin_index:] if begin_index is not None else lines,
        )

    if protocol := _extract_protocol_yaml(request.protocol_lines):
        request.submit_confirmed = True
        return protocol
    if protocol := _extract_protocol_json(request.protocol_lines):
        request.submit_confirmed = True
        return protocol
    return ""


def _conversation_transcript_candidates(
    *,
    preferred_conversation_id: str | None,
    modified_after: float,
) -> list[tuple[str, Path]]:
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not brain_dir.is_dir():
        return []

    if preferred_conversation_id:
        preferred_logs_dir = (
            brain_dir / preferred_conversation_id / ".system_generated" / "logs"
        )
        for filename in ("transcript_full.jsonl", "transcript.jsonl"):
            preferred_path = preferred_logs_dir / filename
            if not preferred_path.is_file():
                continue
            try:
                recently_modified = (
                    preferred_path.stat().st_mtime + _TRANSCRIPT_RECENT_GRACE_SECONDS
                    >= modified_after
                )
            except OSError:
                recently_modified = False
            if recently_modified:
                return [(preferred_conversation_id, preferred_path)]
            break

    candidates: list[tuple[bool, int, str, Path]] = []
    try:
        conversation_dirs = list(brain_dir.iterdir())
    except OSError:
        return []
    for conversation_dir in conversation_dirs:
        if not conversation_dir.is_dir():
            continue
        conversation_id = conversation_dir.name
        logs_dir = conversation_dir / ".system_generated" / "logs"
        transcript_path = logs_dir / "transcript_full.jsonl"
        if not transcript_path.is_file():
            transcript_path = logs_dir / "transcript.jsonl"
        if not transcript_path.is_file():
            continue
        try:
            modified_at = transcript_path.stat().st_mtime
        except OSError:
            continue
        preferred = conversation_id == preferred_conversation_id
        if (
            not preferred
            and modified_at + _TRANSCRIPT_RECENT_GRACE_SECONDS < modified_after
        ):
            continue
        candidates.append(
            (
                preferred,
                int(modified_at * 1_000_000_000),
                conversation_id,
                transcript_path,
            )
        )

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [
        (conversation_id, transcript_path)
        for _, _, conversation_id, transcript_path in candidates[
            :_TRANSCRIPT_CANDIDATE_LIMIT
        ]
    ]


def _protocol_state_after_marker_in_transcript(
    path: Path,
    marker: str,
) -> tuple[bool, str | None]:
    marker_seen = False
    try:
        transcript = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return False, None

    with transcript:
        for raw_line in transcript:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            content = event.get("content")
            content = content if isinstance(content, str) else ""
            if event.get("type") == "USER_INPUT":
                if marker in content:
                    marker_seen = True
                    continue
                if marker_seen:
                    return True, None
            if (
                not marker_seen
                or event.get("source") != "MODEL"
                or event.get("type") != "PLANNER_RESPONSE"
                or not content
            ):
                continue
            lines = content.splitlines()
            if protocol := _extract_protocol_yaml(lines):
                return True, protocol
            if protocol := _extract_protocol_json(lines):
                return True, protocol
    return marker_seen, None


def _protocol_after_marker_in_transcript(path: Path, marker: str) -> str | None:
    return _protocol_state_after_marker_in_transcript(path, marker)[1]


def _recover_protocol_from_transcripts(
    request: PersistentTuiRequest,
    preferred_conversation_id: str | None,
) -> str:
    candidate_groups = [
        _conversation_transcript_candidates(
            preferred_conversation_id=preferred_conversation_id,
            modified_after=request.submitted_wall_at,
        )
    ]
    if preferred_conversation_id:
        candidate_groups.append(
            _conversation_transcript_candidates(
                preferred_conversation_id=None,
                modified_after=request.submitted_wall_at,
            )
        )

    checked_paths: set[Path] = set()
    for candidates in candidate_groups:
        for conversation_id, transcript_path in candidates:
            if transcript_path in checked_paths:
                continue
            checked_paths.add(transcript_path)
            marker_seen, protocol = _protocol_state_after_marker_in_transcript(
                transcript_path,
                request.marker,
            )
            if marker_seen:
                request.resolved_conversation_id = conversation_id
            if protocol:
                return protocol
            if marker_seen:
                return ""
    return ""


class PersistentTuiManager:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.sessions: dict[str, PersistentTuiSession] = {}
        self.pexpect: Any = None
        self.pyte: Any = None
        self._lock = threading.RLock()

    def _load_terminal_libraries(self) -> None:
        if self.pexpect is not None and self.pyte is not None:
            return
        try:
            import pexpect
            import pyte
        except ImportError as exc:
            raise BridgeExecutionError(
                (
                    "Persistent Antigravity terminal mode requires the "
                    "`pexpect` and `pyte` packages."
                ),
                status_code=500,
                code="antigravity_terminal_dependency_missing",
            ) from exc
        self.pexpect = pexpect
        self.pyte = pyte

    @staticmethod
    def _stop_session(session: PersistentTuiSession) -> None:
        session.stop_event.set()

    def _close_session(self, session_key: str, *, wait: bool = True) -> None:
        with self._lock:
            session = self.sessions.pop(session_key, None)
        if session is None:
            return
        self._stop_session(session)
        if wait and not session.closed_event.wait(timeout=3):
            LOGGER.warning(
                "Persistent terminal worker did not stop promptly; forcing exit. "
                "session=%s",
                session_key,
            )
            with contextlib.suppress(Exception):
                session.child.terminate(force=True)
            session.closed_event.wait(timeout=1)

    def cancel_and_wait(self, session_key: str) -> None:
        self._close_session(session_key, wait=True)

    def close_all(self) -> None:
        with self._lock:
            session_keys = list(self.sessions)
        for session_key in session_keys:
            self._close_session(session_key)

    def _evict_if_needed(self) -> None:
        while True:
            with self._lock:
                if len(self.sessions) < self.config.persistent_session_limit:
                    return
                oldest_key = min(
                    self.sessions,
                    key=lambda key: self.sessions[key].last_used_at,
                )
            LOGGER.info(
                "Evicting persistent Antigravity terminal session. session=%s",
                oldest_key,
            )
            self._close_session(oldest_key)

    def _spawn_session(
        self,
        *,
        session_key: str,
        prompt: str,
        conversation: ConversationSession | None,
        initial_request: PersistentTuiRequest,
        agy_model: str | None,
    ) -> PersistentTuiSession:
        self._load_terminal_libraries()
        known_conversation_ids = _conversation_database_ids()
        command = build_antigravity_interactive_command(
            config=self.config,
            prompt=prompt,
            conversation_id=(conversation.conversation_id if conversation else None),
            agy_model=agy_model,
        )
        env = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get(
                "PATH",
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            ),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
        }
        try:
            child = self.pexpect.spawn(
                command[0],
                command[1:],
                cwd=str(self.config.workdir),
                env=env,
                encoding="utf-8",
                codec_errors="replace",
                echo=False,
                dimensions=(
                    self.config.persistent_rows,
                    self.config.persistent_columns,
                ),
                timeout=1,
            )
        except Exception as exc:
            raise BridgeExecutionError(
                f"Could not start the persistent Antigravity terminal: {exc}",
                status_code=500,
                code="antigravity_terminal_start_failed",
            ) from exc

        screen = self.pyte.Screen(
            self.config.persistent_columns,
            self.config.persistent_rows,
        )
        session = PersistentTuiSession(
            child=child,
            screen=screen,
            stream=self.pyte.Stream(screen),
            turn_count=conversation.turn_count if conversation else 0,
            last_used_at=time.time(),
            conversation_id=(conversation.conversation_id if conversation else None),
            known_conversation_ids=known_conversation_ids,
            requests=queue.Queue(),
            stop_event=threading.Event(),
            closed_event=threading.Event(),
            screen_snapshots=deque(maxlen=self.config.persistent_buffer_screens),
            initial_request=initial_request,
        )
        worker = threading.Thread(
            target=self._session_worker,
            args=(session_key, session),
            name=f"agy-tui-{session_key[-32:]}",
            daemon=True,
        )
        session.worker = worker
        self.sessions[session_key] = session
        worker.start()
        return session

    def _write_with_timeout(
        self,
        session: PersistentTuiSession,
        text: str,
    ) -> None:
        file_descriptor = session.child.fileno()
        payload = memoryview(text.encode("utf-8"))
        deadline = time.monotonic() + self.config.persistent_write_timeout_seconds
        was_blocking = os.get_blocking(file_descriptor)
        os.set_blocking(file_descriptor, False)
        try:
            while payload:
                if session.stop_event.is_set():
                    raise BridgeExecutionError(
                        "The persistent terminal request was cancelled.",
                        code="antigravity_terminal_cancelled",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeExecutionError(
                        (
                            "Persistent terminal input was backpressured for more "
                            f"than {self.config.persistent_write_timeout_seconds:.0f} "
                            "seconds."
                        ),
                        status_code=504,
                        code="antigravity_terminal_backpressure",
                    )
                readable, writable, _ = select.select(
                    [file_descriptor],
                    [file_descriptor],
                    [],
                    min(remaining, 0.1),
                )
                if readable:
                    try:
                        chunk = session.child.read_nonblocking(
                            size=65536,
                            timeout=0,
                        )
                    except self.pexpect.TIMEOUT:
                        chunk = ""
                    except self.pexpect.EOF as exc:
                        raise BridgeExecutionError(
                            "The persistent Antigravity terminal reached EOF.",
                            code="antigravity_terminal_eof",
                        ) from exc
                    if chunk:
                        session.stream.feed(chunk)
                if not writable:
                    continue
                try:
                    written = os.write(file_descriptor, payload)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise BridgeExecutionError(
                        "The persistent terminal stopped accepting input.",
                        code="antigravity_terminal_write_failed",
                    )
                payload = payload[written:]
        finally:
            with contextlib.suppress(OSError):
                os.set_blocking(file_descriptor, was_blocking)

    def _fail_session_requests(
        self,
        session: PersistentTuiSession,
        active_request: PersistentTuiRequest | None,
        failure: BaseException,
    ) -> None:
        if active_request is not None and not active_request.future.done():
            active_request.future.set_exception(failure)
        while True:
            try:
                request = session.requests.get_nowait()
            except queue.Empty:
                break
            if not request.future.done():
                request.future.set_exception(failure)

    def _session_worker(
        self,
        session_key: str,
        session: PersistentTuiSession,
    ) -> None:
        active_request = session.initial_request
        session.initial_request = None
        trust_confirmed = False
        failure: BaseException = BridgeExecutionError(
            "The persistent terminal session was closed.",
            code="antigravity_terminal_closed",
        )
        try:
            while not session.stop_event.is_set():
                if active_request is None:
                    try:
                        active_request = session.requests.get_nowait()
                    except queue.Empty:
                        active_request = None
                    else:
                        active_request.started_at = time.monotonic()
                        active_request.last_progress_at = active_request.started_at
                        LOGGER.info(
                            "Submitting prompt to persistent terminal. session=%s",
                            session_key,
                        )
                        self._write_with_timeout(
                            session,
                            (f"\x1b[200~{active_request.marked_prompt}\x1b[201~\r"),
                        )
                        active_request.submitted_at = time.monotonic()
                        active_request.submitted_wall_at = time.time()

                if active_request is not None and active_request.started_at == 0:
                    active_request.started_at = time.monotonic()
                    active_request.submitted_at = active_request.started_at
                    active_request.last_progress_at = active_request.started_at

                if not session.child.isalive():
                    raise BridgeExecutionError(
                        "The persistent Antigravity terminal exited unexpectedly.",
                        code="antigravity_terminal_exited",
                    )
                try:
                    chunk = session.child.read_nonblocking(
                        size=_PERSISTENT_READ_CHARS,
                        timeout=0.1,
                    )
                except self.pexpect.TIMEOUT:
                    chunk = ""
                except self.pexpect.EOF as exc:
                    raise BridgeExecutionError(
                        "The persistent Antigravity terminal reached EOF.",
                        code="antigravity_terminal_eof",
                    ) from exc

                if chunk:
                    session.stream.feed(chunk)
                    if active_request is not None:
                        active_request.last_progress_at = time.monotonic()

                lines = _render_tui_screen(session.screen)
                snapshot = tuple(lines)
                if (
                    not session.screen_snapshots
                    or session.screen_snapshots[-1] != snapshot
                ):
                    session.screen_snapshots.append(snapshot)
                visible = "\n".join(lines)

                if (
                    not trust_confirmed
                    and "Do you trust the contents of this project?" in visible
                ):
                    self._write_with_timeout(session, "\r")
                    trust_confirmed = True

                if active_request is None:
                    continue

                if "Generating..." in visible or "esc to cancel" in visible:
                    active_request.submit_confirmed = True

                terminal_response = _capture_tui_response(active_request, lines)
                if terminal_response:
                    active_request.terminal_protocol = terminal_response
                response = ""
                now = time.monotonic()
                if (
                    (active_request.submit_confirmed or active_request.marker_seen)
                    and now - active_request.last_transcript_poll_at
                    >= _TRANSCRIPT_POLL_SECONDS
                ):
                    active_request.last_transcript_poll_at = now
                    response = _recover_protocol_from_transcripts(
                        active_request,
                        session.conversation_id,
                    )
                    if active_request.resolved_conversation_id:
                        session.conversation_id = (
                            active_request.resolved_conversation_id
                        )
                    if response:
                        LOGGER.info(
                            "Recovered complete response from Antigravity transcript. "
                            "session=%s conversation=%s",
                            session_key,
                            active_request.resolved_conversation_id,
                        )
                if response:
                    if active_request.resolved_conversation_id:
                        session.conversation_id = (
                            active_request.resolved_conversation_id
                        )
                    if (
                        active_request.terminal_protocol
                        and active_request.terminal_protocol != response
                    ):
                        LOGGER.warning(
                            "Discarded an unverified terminal response in favor of "
                            "the matching Antigravity transcript. session=%s",
                            session_key,
                        )
                    if not active_request.future.done():
                        active_request.future.set_result(response)
                    LOGGER.info(
                        "Persistent terminal completed request. session=%s",
                        session_key,
                    )
                    active_request = None
                    continue

                if (
                    active_request.needs_write
                    and not active_request.submit_confirmed
                    and now - active_request.submitted_at
                    >= self.config.persistent_submit_timeout_seconds
                ):
                    raise BridgeExecutionError(
                        (
                            "The persistent terminal did not acknowledge the "
                            "submitted prompt."
                        ),
                        status_code=504,
                        code="antigravity_terminal_submit_timeout",
                    )
                if (
                    active_request.submit_confirmed
                    and now - active_request.last_progress_at
                    >= self.config.persistent_progress_timeout_seconds
                ):
                    response = _recover_protocol_from_transcripts(
                        active_request,
                        session.conversation_id,
                    )
                    if response:
                        if not active_request.future.done():
                            active_request.future.set_result(response)
                        active_request = None
                        continue
                    raise BridgeExecutionError(
                        (
                            "The persistent terminal produced no output progress "
                            f"for {self.config.persistent_progress_timeout_seconds:.0f} "
                            "seconds."
                        ),
                        status_code=504,
                        code="antigravity_terminal_stalled",
                    )
                if now - active_request.started_at >= self.config.timeout_seconds:
                    response = _recover_protocol_from_transcripts(
                        active_request,
                        session.conversation_id,
                    )
                    if response:
                        if not active_request.future.done():
                            active_request.future.set_result(response)
                        active_request = None
                        continue
                    raise BridgeExecutionError(
                        (
                            "Persistent Antigravity terminal timed out after "
                            f"{self.config.timeout_seconds:.0f} seconds."
                        ),
                        status_code=504,
                        code="antigravity_terminal_timeout",
                    )
        except BaseException as exc:
            failure = exc
            if not session.stop_event.is_set():
                LOGGER.warning(
                    "Persistent terminal worker failed. session=%s code=%s error=%s",
                    session_key,
                    getattr(exc, "code", type(exc).__name__),
                    exc,
                )
        finally:
            session.stop_event.set()
            with contextlib.suppress(Exception):
                if session.child.isalive():
                    session.child.terminate(force=True)
            with contextlib.suppress(Exception):
                session.child.close(force=True)
            self._fail_session_requests(session, active_request, failure)
            session.closed_event.set()
            with self._lock:
                if self.sessions.get(session_key) is session:
                    self.sessions.pop(session_key, None)

    def run(
        self,
        *,
        session_key: str,
        prompt: str,
        conversation: ConversationSession | None,
        agy_model: str | None,
    ) -> tuple[str, str | None, int]:
        marker = f"AGY_BRIDGE_REQUEST_{uuid.uuid4().hex}"
        marked_prompt = _marked_tui_prompt(prompt, marker)
        request = PersistentTuiRequest(
            marker=marker,
            marked_prompt=marked_prompt,
            needs_write=False,
            snapshots=deque(maxlen=self.config.persistent_buffer_screens),
        )
        with self._lock:
            session = self.sessions.get(session_key)
            if session is not None:
                expired = (
                    self.config.session_idle_seconds > 0
                    and time.time() - session.last_used_at
                    > self.config.session_idle_seconds
                )
                exhausted = (
                    self.config.session_max_turns > 0
                    and session.turn_count >= self.config.session_max_turns
                )
                if (
                    expired
                    or exhausted
                    or not session.child.isalive()
                    or session.closed_event.is_set()
                ):
                    self._close_session(session_key)
                    session = None

            if session is None:
                self._evict_if_needed()
                session = self._spawn_session(
                    session_key=session_key,
                    prompt=marked_prompt,
                    conversation=conversation,
                    initial_request=request,
                    agy_model=agy_model,
                )
            else:
                request.needs_write = True
                session.requests.put(request)
        try:
            output_text = request.future.result(
                timeout=(
                    self.config.timeout_seconds
                    + self.config.persistent_write_timeout_seconds
                    + 5
                )
            )
        except BaseException:
            recovered_output = _recover_protocol_from_transcripts(
                request,
                session.conversation_id,
            )
            self._close_session(session_key)
            if not recovered_output:
                raise
            output_text = recovered_output

        with self._lock:
            session.turn_count += 1
            session.last_used_at = time.time()
            if request.resolved_conversation_id:
                session.conversation_id = request.resolved_conversation_id
            elif session.conversation_id is None:
                session.conversation_id = _new_conversation_id(
                    session.known_conversation_ids
                )
            return output_text, session.conversation_id, session.turn_count


_TUI_MANAGERS: dict[int, PersistentTuiManager] = {}
_PERSISTENT_SESSION_EXCLUDED_PREFIXES = _BACKGROUND_SESSION_PREFIXES


def _tui_manager(config: BridgeConfig) -> PersistentTuiManager:
    config_key = id(config)
    if config_key not in _TUI_MANAGERS:
        _TUI_MANAGERS[config_key] = PersistentTuiManager(config)
    return _TUI_MANAGERS[config_key]


def _should_use_persistent_tui(
    config: BridgeConfig,
    session_key: str | None,
) -> bool:
    return bool(
        config.persistent_tui
        and config.reuse_conversations
        and session_key
        and not session_key.startswith(_PERSISTENT_SESSION_EXCLUDED_PREFIXES)
    )


def _normalize_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_antigravity_output(
    output_text: str,
    tools: list[dict[str, Any]] | None,
) -> BridgeAssistantResult:
    allowed_names = {
        str(function_data.get("name"))
        for tool in tools or []
        if isinstance(tool, dict)
        and isinstance((function_data := tool.get("function")), dict)
        and function_data.get("name")
    }
    candidate = output_text.strip()
    if protocol_yaml := _extract_protocol_yaml(candidate.splitlines()):
        candidate = protocol_yaml
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        if _PROTOCOL_BEGIN in output_text or _PROTOCOL_END in output_text:
            raise BridgeExecutionError(
                "Antigravity returned an incomplete response envelope.",
                code="antigravity_protocol_invalid",
            )
        return BridgeAssistantResult(content=output_text)

    if not isinstance(payload, dict):
        return BridgeAssistantResult(content=output_text)

    response_type = str(payload.get("type") or "").strip().lower()
    if response_type == "final":
        return BridgeAssistantResult(
            content=_normalize_visible_content(str(payload.get("content") or ""))
        )

    if response_type != "tool_call":
        if _PROTOCOL_BEGIN in output_text or _PROTOCOL_END in output_text:
            raise BridgeExecutionError(
                "Antigravity returned an invalid response envelope.",
                code="antigravity_protocol_invalid",
            )
        return BridgeAssistantResult(content=output_text)

    name = str(payload.get("name") or "").strip()
    if name not in allowed_names:
        return BridgeAssistantResult(
            content=f"I could not use the requested function `{name}`."
        )

    arguments = _normalize_tool_arguments(payload.get("arguments"))
    return BridgeAssistantResult(
        content=None,
        tool_calls=(
            {
                "id": f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            },
        ),
    )


def _tail_text(stdout_text: str, stderr_text: str) -> str:
    for raw_text in (stderr_text, stdout_text):
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if lines:
            return "\n".join(lines[-8:])
    return "Antigravity CLI exited without a usable error message."


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None or process.pid is None:
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


def _message_matches_marker(message: dict[str, Any], marker: dict[str, Any]) -> bool:
    if message.get("role") != "assistant":
        return False
    if marker.get("tool_calls"):
        return message.get("tool_calls") == marker.get("tool_calls")
    return message.get("content") == marker.get("content")


def _messages_after_last_response(
    messages: list[dict[str, Any]],
    marker: dict[str, Any],
) -> list[dict[str, Any]]:
    control_messages = [
        message
        for message in messages
        if message.get("role") in {"system", "developer"}
    ]

    def with_current_controls(
        delta: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            *control_messages,
            *[
                message
                for message in delta
                if message.get("role") not in {"system", "developer"}
            ],
        ]

    for index in range(len(messages) - 1, -1, -1):
        if _message_matches_marker(messages[index], marker):
            delta = messages[index + 1 :]
            if delta:
                return with_current_controls(delta)

    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") in {"user", "tool"}:
            return with_current_controls([messages[index]])
    return messages


def _conversation_database_paths() -> dict[str, Path]:
    conversation_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    return {path.stem: path for path in conversation_dir.glob("*.db") if path.is_file()}


def _conversation_database_ids() -> set[str]:
    return set(_conversation_database_paths())


def _new_conversation_id(known_ids: set[str]) -> str | None:
    candidates = {
        conversation_id: path
        for conversation_id, path in _conversation_database_paths().items()
        if conversation_id not in known_ids
    }
    if not candidates:
        return None

    def modification_time(item: tuple[str, Path]) -> int:
        with contextlib.suppress(OSError):
            return item[1].stat().st_mtime_ns
        return 0

    return max(candidates.items(), key=modification_time)[0]


def _latest_conversation_id(config: BridgeConfig) -> str | None:
    cache_path = (
        Path.home()
        / ".gemini"
        / "antigravity-cli"
        / "cache"
        / "last_conversations.json"
    )
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    conversation_id = payload.get(str(config.workdir))
    if isinstance(conversation_id, str) and conversation_id.strip():
        return conversation_id.strip()
    return None


def _assistant_marker(result: BridgeAssistantResult) -> dict[str, Any]:
    marker: dict[str, Any] = {
        "role": "assistant",
        "content": result.content,
    }
    if result.tool_calls:
        marker["tool_calls"] = list(result.tool_calls)
    return marker


def _save_conversation_session(
    *,
    config: BridgeConfig,
    session_key: str,
    conversation_id: str,
    turn_count: int,
    result: BridgeAssistantResult,
    resumed: bool,
) -> None:
    _registry(config).put(
        session_key,
        ConversationSession(
            conversation_id=conversation_id,
            turn_count=turn_count,
            last_used_at=time.time(),
            assistant_marker=_assistant_marker(result),
        ),
    )
    LOGGER.info(
        (
            "Saved Antigravity conversation state. session=%s "
            "conversation=%s turn=%s resumed=%s"
        ),
        session_key,
        conversation_id,
        turn_count,
        resumed,
    )


def _reusable_session(
    config: BridgeConfig,
    session_key: str | None,
) -> ConversationSession | None:
    if not config.reuse_conversations or not session_key:
        return None
    session = _registry(config).get(session_key)
    if not session:
        return None
    expired = (
        config.session_idle_seconds > 0
        and time.time() - session.last_used_at > config.session_idle_seconds
    )
    exhausted = (
        config.session_max_turns > 0 and session.turn_count >= config.session_max_turns
    )
    if expired or exhausted:
        LOGGER.info(
            "Rotating Antigravity conversation. session=%s reason=%s turns=%s",
            session_key,
            "idle" if expired else "turn_limit",
            session.turn_count,
        )
        _registry(config).remove(session_key)
        return None
    return session


async def run_antigravity_prompt(
    *,
    prompt: str,
    requested_model: str,
    config: BridgeConfig,
    session_key: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    **_: Any,
) -> BridgeAssistantResult:
    del requested_model
    request_started_at = time.monotonic()
    persistent_failure: Exception | None = None
    prompt_messages = messages or []
    model_route, agy_model = _select_model_route(
        prompt_messages,
        config=config,
        session_key=session_key,
    )
    if model_route == "gpt":
        return await _chatgpt_web_completion(prompt_messages)
    if config.manager_base_url:
        manager_model = (
            agy_model if model_route == "opus" else config.manager_model or agy_model
        )
        if not manager_model:
            raise BridgeExecutionError(
                "Antigravity Manager backend requires a real model ID.",
                status_code=500,
                code="antigravity_manager_model_missing",
            )
        LOGGER.info(
            "Selected Antigravity Manager route. session=%s route=%s model=%s",
            session_key,
            model_route,
            manager_model,
        )
        return await _antigravity_manager_completion(
            prompt_messages,
            tools=tools,
            tool_choice=tool_choice,
            model=manager_model,
            config=config,
        )
    routed_session_key = _model_scoped_session_key(session_key, model_route)
    session = _reusable_session(config, routed_session_key)
    LOGGER.info(
        "Selected Antigravity model route. session=%s route=%s agy_model=%s",
        session_key,
        model_route,
        agy_model or "global-default",
    )
    if session and prompt_messages:
        prompt_messages = _messages_after_last_response(
            prompt_messages,
            session.assistant_marker,
        )
        prompt = build_antigravity_prompt(
            prompt_messages,
            tools=tools,
            tool_choice=tool_choice,
        )

    if _should_use_persistent_tui(config, routed_session_key):
        manager = _tui_manager(config)
        try:
            output_text, conversation_id, turn_count = await asyncio.to_thread(
                manager.run,
                session_key=routed_session_key,
                prompt=prompt,
                conversation=session,
                agy_model=agy_model,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(manager.cancel_and_wait, routed_session_key)
            )
            raise
        except Exception as exc:
            persistent_failure = exc
            LOGGER.warning(
                (
                    "Persistent Antigravity terminal failed; falling back to "
                    "print mode. session=%s code=%s error=%s"
                ),
                routed_session_key,
                getattr(exc, "code", type(exc).__name__),
                exc,
            )
        else:
            result = parse_antigravity_output(output_text, tools)
            if conversation_id:
                _save_conversation_session(
                    config=config,
                    session_key=routed_session_key,
                    conversation_id=conversation_id,
                    turn_count=turn_count,
                    result=result,
                    resumed=bool(session),
                )
            return result

    remaining_timeout = config.timeout_seconds - (time.monotonic() - request_started_at)
    if remaining_timeout <= 5 and persistent_failure is not None:
        raise persistent_failure

    command = build_antigravity_command(
        config=config,
        prompt=prompt,
        conversation_id=session.conversation_id if session else None,
        agy_model=agy_model,
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=config.workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise BridgeExecutionError(
            f"Antigravity CLI was not found: {config.agy_bin}",
            status_code=500,
            code="antigravity_not_found",
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=max(remaining_timeout, 1),
        )
    except asyncio.CancelledError:
        await _kill_process_group(process)
        raise
    except TimeoutError as exc:
        await _kill_process_group(process)
        raise BridgeExecutionError(
            (
                "Antigravity request timed out after "
                f"{config.timeout_seconds:.0f} total seconds."
            ),
            status_code=504,
            code="antigravity_timeout",
        ) from exc

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")

    if process.returncode != 0:
        raise BridgeExecutionError(
            _tail_text(stdout_text, stderr_text),
            code="antigravity_execution_failed",
        )

    output_text = stdout_text.strip()
    if not output_text:
        raise BridgeExecutionError(
            "Antigravity completed but did not produce an assistant message.",
            code="antigravity_empty_response",
        )

    result = parse_antigravity_output(output_text, tools)
    if config.reuse_conversations and routed_session_key:
        conversation_id = (
            session.conversation_id if session else _latest_conversation_id(config)
        )
        if conversation_id:
            _save_conversation_session(
                config=config,
                session_key=routed_session_key,
                conversation_id=conversation_id,
                turn_count=(session.turn_count if session else 0) + 1,
                result=result,
                resumed=bool(session),
            )
        else:
            LOGGER.warning(
                "Could not discover the Antigravity conversation ID for session=%s",
                routed_session_key,
            )

    return result


def create_app(config: BridgeConfig) -> Quart:
    def _gate_bypass(
        *,
        messages: list[dict[str, Any]],
        session_key: str | None,
    ) -> bool:
        # The GPT route only forwards HTTP to the chat-bridge (which queues on its
        # own side) and returns before any agy session or terminal work, so it has
        # no reason to hold the agy slot.
        route, _ = _select_model_route(messages, config=config, session_key=session_key)
        return route == "gpt"

    app = create_openai_compatible_app(
        config,
        bridge_name="antigravity-openai-bridge",
        backend_name=(
            "Antigravity Manager" if config.manager_base_url else "Antigravity CLI"
        ),
        model_owner="antigravity-bridge",
        prompt_builder=build_antigravity_prompt,
        prompt_runner=run_antigravity_prompt,
        gate_bypass=_gate_bypass,
    )

    @app.after_serving
    async def close_persistent_terminals() -> None:
        manager = _TUI_MANAGERS.pop(id(config), None)
        if manager is not None:
            await asyncio.to_thread(manager.close_all)

    return app


def parse_args(argv: list[str] | None = None) -> BridgeConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Expose a local OpenAI-compatible endpoint backed by Antigravity "
            "Manager or the legacy Antigravity CLI."
        )
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("ANTIGRAVITY_BRIDGE_HOST", "127.0.0.1"),
        help="Bind address for the local bridge server.",
    )
    parser.add_argument(
        "--port",
        default=int(os.environ.get("ANTIGRAVITY_BRIDGE_PORT", "8791")),
        type=int,
        help="Bind port for the local bridge server.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTIGRAVITY_BRIDGE_MODEL", "antigravity"),
        help="Default OpenAI-compatible model ID advertised by the bridge.",
    )
    parser.add_argument(
        "--advertise-model",
        action="append",
        default=[],
        help="Additional model IDs to expose from /v1/models.",
    )
    parser.add_argument(
        "--agy-bin",
        default=os.environ.get("ANTIGRAVITY_BRIDGE_AGY_BIN", "agy"),
        help="Antigravity CLI binary or wrapper to invoke.",
    )
    parser.add_argument(
        "--agy-model",
        default=os.environ.get("ANTIGRAVITY_BRIDGE_AGY_MODEL") or None,
        help="Optional real Antigravity model passed to `agy --model`.",
    )
    parser.add_argument(
        "--opus-model",
        default=(
            os.environ.get("ANTIGRAVITY_BRIDGE_OPUS_MODEL", _DEFAULT_OPUS_MODEL) or None
        ),
        help=(
            "Antigravity model used when the latest user message explicitly requests "
            "Opus. Set an empty value to disable explicit Opus routing."
        ),
    )
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high"],
        default=os.environ.get("ANTIGRAVITY_BRIDGE_EFFORT") or None,
        help="Optional reasoning effort passed to `agy --effort`.",
    )
    parser.add_argument(
        "--manager-base-url",
        default=os.environ.get("ANTIGRAVITY_MANAGER_BASE_URL") or None,
        help=(
            "Optional Antigravity Manager base URL. When set, Manager HTTP replaces "
            "the legacy agy CLI backend. The value may end at the host, /v1, or "
            "/v1/chat/completions."
        ),
    )
    parser.add_argument(
        "--manager-api-key",
        default=os.environ.get("ANTIGRAVITY_MANAGER_API_KEY") or None,
        help="API Key used to authenticate to Antigravity Manager.",
    )
    parser.add_argument(
        "--manager-model",
        default=os.environ.get("ANTIGRAVITY_MANAGER_MODEL") or None,
        help="Real Manager model ID used by the default Antigravity route.",
    )
    parser.add_argument(
        "--manager-timeout",
        default=float(os.environ.get("ANTIGRAVITY_MANAGER_TIMEOUT", "300")),
        type=float,
        help="Maximum wait time for one Antigravity Manager HTTP request.",
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("ANTIGRAVITY_BRIDGE_WORKDIR", os.getcwd()),
        help="Dedicated working directory used for Antigravity executions.",
    )
    parser.add_argument(
        "--timeout",
        default=float(os.environ.get("ANTIGRAVITY_BRIDGE_TIMEOUT", "120")),
        type=float,
        help="Maximum bridge wait time for one Antigravity response.",
    )
    parser.add_argument(
        "--print-timeout",
        default=float(os.environ.get("ANTIGRAVITY_BRIDGE_PRINT_TIMEOUT", "150")),
        type=float,
        help="Timeout forwarded to `agy --print-timeout`.",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("ANTIGRAVITY_BRIDGE_AUTH_TOKEN") or None,
        help="Optional bearer token required by the bridge.",
    )
    parser.add_argument(
        "--stream-chunk-chars",
        default=int(os.environ.get("ANTIGRAVITY_BRIDGE_STREAM_CHUNK_CHARS", "64")),
        type=int,
        help="Pseudo-stream chunk size for stream=true requests.",
    )
    parser.add_argument(
        "--max-parallel-requests",
        default=int(os.environ.get("ANTIGRAVITY_BRIDGE_MAX_PARALLEL", "1")),
        type=int,
        help="Maximum number of concurrent Antigravity executions.",
    )
    parser.add_argument(
        "--disable-sandbox",
        action="store_true",
        default=(
            os.environ.get("ANTIGRAVITY_BRIDGE_SANDBOX", "true").lower() == "false"
        ),
        help="Do not pass `--sandbox` to Antigravity CLI.",
    )
    parser.add_argument(
        "--reuse-conversations",
        action="store_true",
        default=(
            os.environ.get("ANTIGRAVITY_BRIDGE_REUSE_CONVERSATIONS", "false").lower()
            == "true"
        ),
        help="Reuse one Antigravity conversation per upstream session ID.",
    )
    parser.add_argument(
        "--session-max-turns",
        default=int(os.environ.get("ANTIGRAVITY_BRIDGE_SESSION_MAX_TURNS", "20")),
        type=int,
        help="Rotate a reused Antigravity conversation after this many bridge calls.",
    )
    parser.add_argument(
        "--session-idle-seconds",
        default=float(
            os.environ.get("ANTIGRAVITY_BRIDGE_SESSION_IDLE_SECONDS", "21600")
        ),
        type=float,
        help="Rotate a reused conversation after this many idle seconds.",
    )
    parser.add_argument(
        "--session-state-file",
        default=os.environ.get("ANTIGRAVITY_BRIDGE_SESSION_STATE_FILE") or None,
        help="JSON file used to persist upstream session to conversation mappings.",
    )
    parser.add_argument(
        "--persistent-tui",
        action="store_true",
        default=(
            os.environ.get("ANTIGRAVITY_BRIDGE_PERSISTENT_TUI", "false").lower()
            == "true"
        ),
        help="Keep eligible Antigravity interactive terminals alive between turns.",
    )
    parser.add_argument(
        "--persistent-session-limit",
        default=int(
            os.environ.get(
                "ANTIGRAVITY_BRIDGE_PERSISTENT_SESSION_LIMIT",
                "1",
            )
        ),
        type=int,
        help="Maximum number of live Antigravity terminal sessions.",
    )
    parser.add_argument(
        "--persistent-columns",
        default=int(os.environ.get("ANTIGRAVITY_BRIDGE_PERSISTENT_COLUMNS", "160")),
        type=int,
        help="Virtual terminal width used to render Antigravity replies.",
    )
    parser.add_argument(
        "--persistent-rows",
        default=int(os.environ.get("ANTIGRAVITY_BRIDGE_PERSISTENT_ROWS", "50")),
        type=int,
        help="Virtual terminal height; history is retained in an application buffer.",
    )
    parser.add_argument(
        "--persistent-write-timeout",
        default=float(
            os.environ.get("ANTIGRAVITY_BRIDGE_PERSISTENT_WRITE_TIMEOUT", "2")
        ),
        type=float,
        help="Maximum time allowed for a non-blocking terminal write.",
    )
    parser.add_argument(
        "--persistent-submit-timeout",
        default=float(
            os.environ.get("ANTIGRAVITY_BRIDGE_PERSISTENT_SUBMIT_TIMEOUT", "5")
        ),
        type=float,
        help="Maximum time for an interactive prompt to enter generating state.",
    )
    parser.add_argument(
        "--persistent-progress-timeout",
        default=float(
            os.environ.get("ANTIGRAVITY_BRIDGE_PERSISTENT_PROGRESS_TIMEOUT", "60")
        ),
        type=float,
        help="Maximum time an active terminal response may make no output progress.",
    )
    parser.add_argument(
        "--persistent-buffer-screens",
        default=int(
            os.environ.get("ANTIGRAVITY_BRIDGE_PERSISTENT_BUFFER_SCREENS", "256")
        ),
        type=int,
        help="Number of changed terminal snapshots retained per session.",
    )

    args = parser.parse_args(argv)
    if args.manager_base_url and not args.manager_api_key:
        parser.error("--manager-api-key is required with --manager-base-url")
    if args.manager_base_url and not args.manager_model:
        parser.error("--manager-model is required with --manager-base-url")
    advertised_models = tuple(
        dict.fromkeys([args.model, *[model for model in args.advertise_model if model]])
    )
    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    session_state_file = (
        Path(args.session_state_file).expanduser().resolve()
        if args.session_state_file
        else workdir / "bridge_sessions.json"
    )

    return BridgeConfig(
        host=args.host,
        port=args.port,
        default_model=args.model,
        advertised_models=advertised_models,
        agy_bin=args.agy_bin,
        agy_model=args.agy_model,
        opus_model=args.opus_model,
        effort=args.effort,
        workdir=workdir,
        timeout_seconds=max(args.timeout, 1),
        print_timeout_seconds=max(args.print_timeout, 1),
        auth_token=args.auth_token,
        stream_chunk_chars=max(args.stream_chunk_chars, 1),
        max_parallel_requests=max(args.max_parallel_requests, 1),
        sandbox=not args.disable_sandbox,
        reuse_conversations=args.reuse_conversations,
        session_max_turns=max(args.session_max_turns, 1),
        session_idle_seconds=max(args.session_idle_seconds, 0),
        session_state_file=session_state_file,
        persistent_tui=args.persistent_tui,
        persistent_session_limit=max(args.persistent_session_limit, 1),
        persistent_columns=max(args.persistent_columns, 80),
        persistent_rows=max(args.persistent_rows, 40),
        persistent_write_timeout_seconds=max(args.persistent_write_timeout, 0.1),
        persistent_submit_timeout_seconds=max(args.persistent_submit_timeout, 0.5),
        persistent_progress_timeout_seconds=max(
            args.persistent_progress_timeout,
            1,
        ),
        persistent_buffer_screens=max(args.persistent_buffer_screens, 8),
        manager_base_url=args.manager_base_url,
        manager_api_key=args.manager_api_key,
        manager_model=args.manager_model,
        manager_timeout_seconds=max(args.manager_timeout, 1),
    )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = parse_args(argv)
    app = create_app(config)
    LOGGER.info(
        "Starting Antigravity bridge on http://%s:%s (model=%s, backend=%s, workdir=%s)",
        config.host,
        config.port,
        config.default_model,
        "manager" if config.manager_base_url else "cli",
        config.workdir,
    )
    app.run(host=config.host, port=config.port)


if __name__ == "__main__":
    main()
