from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quart import Quart, Response, jsonify, request, send_file

LOGGER = logging.getLogger(__name__)

DEFAULT_CHAT_URL = "https://www.doubao.com/chat/"
DEFAULT_MODEL = "doubao-web"
DEFAULT_INPUT_SELECTORS = (
    "textarea",
    "[role='textbox']",
    "[contenteditable='true']",
)
DEFAULT_NEW_CHAT_LABELS = (
    "新对话",
    "新聊天",
    "开启新对话",
    "开始新对话",
    "重新开始",
)
DEFAULT_NEW_CHAT_SELECTORS = ("[data-testid='create_conversation_button']",)
LOGIN_HINTS = (
    "登录后免费使用完整功能",
    "打开 豆包 App 扫码登录",
    "打开 豆包 App扫码登录",
    "下一步",
)
HOME_SCREEN_HINTS = (
    "你好，我是豆包",
    "内容由豆包 AI 生成",
    "下载豆包电脑版",
    "Generate Image",
    "Help Me Write",
    "Translate",
)
LOGIN_BUTTON_TEXT = "登录"
SCREENSHOT_FILENAME = "doubao-login.png"
KNOWN_UI_NOISE = {
    "豆包",
    "AI 创作",
    "历史对话",
    "关于豆包",
    "内容由豆包 AI 生成",
    "下载豆包电脑版",
    "登录",
    "发送",
    "停止生成",
    "重新生成",
    "深度思考",
    "联网搜索",
    "上传图片",
    "新对话",
    "快速",
    "图像生成",
    "视频生成",
    "帮我写作",
    "翻译",
    "编程",
    "深入研究",
    "更多",
    "1",
}
LOADING_HINTS = (
    "思考中",
    "生成中",
    "停止生成",
    "正在搜索",
    "联网搜索中",
    "继续生成",
)
QUESTION_PREFIXES = (
    "什么",
    "为啥",
    "为什么",
    "怎么",
    "怎样",
    "如何",
    "哪个",
    "哪种",
    "哪类",
    "哪里",
    "哪儿",
    "谁",
    "多少",
    "几",
    "能不能",
    "可不可以",
    "是否",
    "是不是",
    "会不会",
    "要不要",
    "要怎么",
    "需不需要",
    "有没有",
)
SUGGESTION_PREFIXES = (
    "推荐一些",
    "推荐几个",
    "推荐点",
    "推荐下",
    "有什么适合",
    "换个",
    "来个",
    "分享一个",
    "讲个",
)
TITLE_CHIP_PREFIXES = (
    "回答",
    "科普",
    "总结",
    "分析",
    "解释",
    "翻译",
    "介绍",
    "推荐",
    "整理",
    "提炼",
    "点评",
    "列出",
    "归纳",
    "比较",
    "对比",
    "写",
    "改写",
    "润色",
    "扩写",
    "续写",
)
TITLE_CHIP_FILLER_TOKENS = (
    "请",
    "帮",
    "帮我",
    "给我",
    "给大家",
    "给",
    "我",
    "我们",
    "大家",
    "一下",
    "一段",
    "一个",
    "一点",
    "一些",
    "最新",
    "最近",
    "这个",
    "那个",
    "这",
    "那",
    "关于",
    "有关",
    "一下子",
    "的",
)
ROLE_MARKER_PATTERN = re.compile(
    r"^\[(system|user|assistant|tool|developer)\]$", re.IGNORECASE
)
REFERENCE_BADGE_PATTERN = re.compile(r"^参考\s*\d+\s*篇资料$")
RETRIEVAL_STATUS_PATTERN = re.compile(
    r"^(已)?找到\s*\d+\s*(篇资料|条资料|篇文章|篇参考资料|个结果|条结果|个网页|条网页)(，.*)?$"
)
AI_DISCLAIMER_PATTERN = re.compile(r"^本[问次条]?回答由AI生成.*仅供参考.*$")
PROMPT_LEAK_HINTS = (
    "<system_reminder>",
    "Current datetime:",
    "You are running in Safe Mode.",
    "mcp startup:",
)


@dataclass(frozen=True)
class BridgeConfig:
    host: str
    port: int
    default_model: str
    advertised_models: tuple[str, ...]
    auth_token: str | None
    stream_chunk_chars: int
    max_parallel_requests: int
    timeout_seconds: float
    ready_timeout_seconds: float
    browser_executable: str | None
    user_data_dir: Path
    login_screenshot_path: Path
    headless: bool
    chat_url: str
    input_selectors: tuple[str, ...]
    new_chat_labels: tuple[str, ...]
    new_chat_selectors: tuple[str, ...]
    prompt_capture_path: Path | None
    incident_capture_dir: Path | None


@dataclass(frozen=True)
class ChatSnapshot:
    body_text: str
    blocks: tuple[str, ...]


class BridgeExecutionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "bridge_execution_failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


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
            return f"[Image omitted: {_extract_image_url(content)}]"
        return _safe_json(content)

    if isinstance(content, list):
        rendered_parts: list[str] = []
        for part in content:
            rendered = render_message_content(part).strip()
            if rendered:
                rendered_parts.append(rendered)
        return "\n".join(rendered_parts)

    return str(content)


def build_chat_prompt(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = render_message_content(message.get("content")).strip()
        if not content:
            continue
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts).strip()


def _capture_prompt(
    capture_path: Path | None,
    *,
    prompt: str,
    requested_model: str,
    message_count: int,
) -> None:
    if capture_path is None:
        return
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "model": requested_model,
        "message_count": message_count,
        "prompt_chars": len(prompt),
        "prompt": prompt,
    }
    with capture_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _capture_incident(
    incident_dir: Path | None,
    *,
    prompt: str,
    requested_model: str,
    message_count: int,
    status_code: int,
    code: str,
    error_message: str,
    details: dict[str, Any] | None = None,
) -> None:
    if incident_dir is None:
        return

    incident_dir.mkdir(parents=True, exist_ok=True)
    incident_id = (
        f"{time.strftime('%Y%m%dT%H%M%S', time.localtime())}-{uuid.uuid4().hex[:8]}"
    )
    prompt_path = incident_dir / f"{incident_id}.prompt.txt"
    metadata_path = incident_dir / f"{incident_id}.json"

    prompt_path.write_text(prompt, encoding="utf-8")
    payload = {
        "incident_id": incident_id,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "model": requested_model,
        "message_count": message_count,
        "prompt_chars": len(prompt),
        "prompt_path": str(prompt_path),
        "status_code": status_code,
        "code": code,
        "error_message": error_message,
        "details": details or {},
    }
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_chat_completion_response(
    *,
    content: str,
    model: str,
    completion_id: str,
    created: int,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
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
    content: str,
    model: str,
    completion_id: str,
    created: int,
    chunk_size: int,
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

    for chunk in _chunk_text(content, chunk_size):
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

    payloads.append(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    return payloads


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


def _require_auth(config: BridgeConfig) -> tuple[Response, int] | None:
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


def _model_payload(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "doubao-web-bridge",
    }


def _normalize_text(text: str) -> str:
    normalized = text.replace("\u00a0", " ")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _strip_prompt_echo(text: str, prompt: str) -> str:
    prompt = _normalize_text(prompt)
    text = _normalize_text(text)
    if not prompt or not text:
        return text
    if text == prompt:
        return ""
    if text.startswith(prompt + "\n"):
        return _normalize_text(text[len(prompt) :])
    return text


def _looks_like_loading(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    lowered = normalized.lower()
    if any(hint.lower() in lowered for hint in LOADING_HINTS):
        return True
    lines = [line for line in normalized.splitlines() if line]
    return bool(lines) and all(
        RETRIEVAL_STATUS_PATTERN.fullmatch(line) for line in lines
    )


def _remove_ui_noise_lines(text: str) -> str:
    filtered_lines = [
        line
        for line in (_normalize_text(part) for part in text.splitlines())
        if (
            line
            and line not in KNOWN_UI_NOISE
            and not REFERENCE_BADGE_PATTERN.fullmatch(line)
            and not RETRIEVAL_STATUS_PATTERN.fullmatch(line)
            and not AI_DISCLAIMER_PATTERN.fullmatch(line)
        )
    ]
    return _normalize_text("\n".join(filtered_lines))


def _looks_like_home_screen(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized or "你好，我是豆包" not in normalized:
        return False
    return sum(1 for hint in HOME_SCREEN_HINTS if hint in normalized) >= 3


def _prompt_anchor(prompt: str) -> str:
    lines = [
        line.strip() for line in _normalize_text(prompt).splitlines() if line.strip()
    ]
    for line in reversed(lines):
        if re.fullmatch(r"\[[^\]]+\]", line):
            continue
        return line
    return ""


def _extract_block_after_prompt(blocks: tuple[str, ...], prompt: str) -> str:
    anchor = _prompt_anchor(prompt)
    if not anchor:
        return ""

    anchor_index = -1
    normalized_blocks = [_normalize_text(block) for block in blocks]
    for idx, block in enumerate(normalized_blocks):
        if anchor == block or anchor in block:
            anchor_index = idx

    if anchor_index < 0:
        return ""

    for block in normalized_blocks[anchor_index + 1 :]:
        if not block or block in KNOWN_UI_NOISE:
            continue
        if _looks_like_home_screen(block):
            continue
        if anchor == block or anchor in block:
            continue
        return _remove_ui_noise_lines(block)

    return ""


def _looks_like_follow_up_suggestion(line: str) -> bool:
    normalized = _normalize_text(line)
    if not normalized or normalized in KNOWN_UI_NOISE:
        return False

    stripped = normalized.lstrip("-*0123456789. ")
    if len(stripped) > 36:
        return False
    if any(stripped.startswith(prefix) for prefix in SUGGESTION_PREFIXES):
        return True
    if any(punct in stripped for punct in ("，", "。", "！", "~", "～")):
        return False

    if stripped.endswith(("？", "?")):
        return True

    if any(stripped.startswith(prefix) for prefix in QUESTION_PREFIXES):
        return True

    return any(
        marker in stripped
        for marker in ("什么", "为什么", "怎么", "如何", "吗", "么", "呢")
    )


def _compact_hint_text(text: str) -> str:
    compact = _normalize_text(text)
    for token in TITLE_CHIP_FILLER_TOKENS:
        compact = compact.replace(token, "")
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", compact)
    return compact


def _looks_like_title_chip(line: str, prompt: str) -> bool:
    normalized = _normalize_text(line)
    if not normalized or "\n" in normalized:
        return False

    stripped = normalized.strip()
    if len(stripped) > 12:
        return False
    if any(
        punct in stripped
        for punct in ("？", "?", "！", "!", "。", "，", ",", "：", ":")
    ):
        return False
    if not any(stripped.startswith(prefix) for prefix in TITLE_CHIP_PREFIXES):
        return False

    compact_line = _compact_hint_text(stripped)
    compact_prompt = _compact_hint_text(prompt)
    if not compact_line or not compact_prompt:
        return False

    return compact_line in compact_prompt


def _remove_title_chip_lines(text: str, prompt: str) -> str:
    filtered_lines = [
        line
        for line in (_normalize_text(part) for part in text.splitlines())
        if line and not _looks_like_title_chip(line, prompt)
    ]
    return _normalize_text("\n".join(filtered_lines))


def _strip_follow_up_suggestions(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""

    lines = [line for line in normalized.splitlines() if line]
    if len(lines) < 3:
        return normalized

    suggestion_count = 0
    idx = len(lines) - 1
    while idx >= 0 and _looks_like_follow_up_suggestion(lines[idx]):
        suggestion_count += 1
        idx -= 1

    if suggestion_count < 2 or idx < 0:
        return normalized

    kept_lines = lines[: idx + 1]
    if not any(not _looks_like_follow_up_suggestion(line) for line in kept_lines):
        return normalized

    return _normalize_text("\n".join(kept_lines))


def _truncate_repeated_answer_bundle(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""

    lines = [line for line in normalized.splitlines() if line]
    if len(lines) < 4:
        return normalized

    first_line = lines[0]
    for idx in range(1, len(lines)):
        if lines[idx] != first_line:
            continue

        middle = lines[1:idx]
        tail = lines[idx + 1 :]
        if not middle or not any(
            _looks_like_follow_up_suggestion(line) for line in middle
        ):
            continue
        if tail and not all(
            line == first_line or _looks_like_follow_up_suggestion(line)
            for line in tail
        ):
            continue
        return _normalize_text("\n".join(lines[:idx]))

    return normalized


def _truncate_exact_repeat(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""

    lines = [line for line in normalized.splitlines() if line]
    if len(lines) < 2 or len(lines) % 2 != 0:
        return normalized

    half = len(lines) // 2
    if lines[:half] == lines[half:]:
        return _normalize_text("\n".join(lines[:half]))

    return normalized


def _sanitize_prompt_leak(text: str, prompt: str) -> str:
    normalized = _normalize_text(text)
    prompt = _normalize_text(prompt)
    if not normalized:
        return ""

    if prompt and prompt in normalized:
        normalized = _normalize_text(normalized.replace(prompt, ""))
        if not normalized:
            return ""

    lines = [line for line in normalized.splitlines() if line]
    first_marker_idx = next(
        (idx for idx, line in enumerate(lines) if ROLE_MARKER_PATTERN.fullmatch(line)),
        None,
    )
    if first_marker_idx is not None:
        if first_marker_idx == 0:
            return ""
        normalized = _normalize_text("\n".join(lines[:first_marker_idx]))
        if not normalized:
            return ""

    if any(ROLE_MARKER_PATTERN.fullmatch(line) for line in normalized.splitlines()):
        return ""

    lowered = normalized.lower()
    if any(hint.lower() in lowered for hint in PROMPT_LEAK_HINTS):
        return ""

    return normalized


def _clean_candidate(text: str, prompt: str) -> str:
    cleaned = _normalize_text(text)
    cleaned = _strip_prompt_echo(cleaned, prompt)
    cleaned = _remove_ui_noise_lines(cleaned)
    cleaned = _remove_title_chip_lines(cleaned, prompt)
    cleaned = _truncate_exact_repeat(cleaned)
    cleaned = _truncate_repeated_answer_bundle(cleaned)
    cleaned = _strip_follow_up_suggestions(cleaned)
    cleaned = _sanitize_prompt_leak(cleaned, prompt)
    cleaned = _remove_title_chip_lines(cleaned, prompt)
    return _normalize_text(cleaned)


def _common_prefix_len(left: list[str], right: list[str]) -> int:
    idx = 0
    while idx < len(left) and idx < len(right) and left[idx] == right[idx]:
        idx += 1
    return idx


def derive_response_text(
    *,
    before: ChatSnapshot,
    after: ChatSnapshot,
    prompt: str,
) -> str:
    before_body = _normalize_text(before.body_text)
    after_body = _normalize_text(after.body_text)
    prompt = _normalize_text(prompt)

    anchored_block = _extract_block_after_prompt(after.blocks, prompt)
    if anchored_block and not _looks_like_loading(anchored_block):
        anchored_block = _clean_candidate(anchored_block, prompt)
        if anchored_block and not _looks_like_loading(anchored_block):
            return anchored_block

    if before_body and after_body.startswith(before_body):
        appended = _normalize_text(after_body[len(before_body) :])
        appended = _clean_candidate(appended, prompt)
        if appended and not _looks_like_loading(appended):
            return appended

    prefix_len = _common_prefix_len(list(before.blocks), list(after.blocks))
    candidate_blocks = [_normalize_text(block) for block in after.blocks[prefix_len:]]
    filtered: list[str] = []
    for block in candidate_blocks:
        if not block or block in KNOWN_UI_NOISE:
            continue
        stripped = _strip_prompt_echo(block, prompt)
        if not stripped or stripped in KNOWN_UI_NOISE:
            continue
        filtered.append(stripped)

    combined = _normalize_text("\n\n".join(filtered))
    combined = _clean_candidate(combined, prompt)
    if combined and not _looks_like_loading(combined):
        return combined

    return ""


class DoubaoBrowserBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._startup_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    async def start(self) -> None:
        async with self._startup_lock:
            if self._context is not None:
                return

            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise BridgeExecutionError(
                    "Playwright is not installed in the bridge environment.",
                    status_code=500,
                    code="playwright_missing",
                ) from exc

            playwright = await async_playwright().start()
            try:
                self._context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._config.user_data_dir),
                    executable_path=self._config.browser_executable,
                    headless=self._config.headless,
                    ignore_default_args=["--enable-automation"],
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await playwright.stop()
                raise
            self._playwright = playwright
            if self._context.pages:
                self._page = self._context.pages[0]

    async def stop(self) -> None:
        self._page = None
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
            self._context = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def warm_up(self) -> None:
        await self.start()
        async with self._request_lock:
            if self._config.headless:
                page = await self._open_page()
            else:
                assert self._context is not None
                page = await self._context.new_page()
                self._page = page
                await page.goto(self._config.chat_url, wait_until="domcontentloaded")
                with contextlib.suppress(Exception):
                    await page.bring_to_front()
                with contextlib.suppress(Exception):
                    await page.evaluate("() => { window.focus?.(); }")
            await self._wait_for_page_ready(
                page,
                timeout=max(self._config.ready_timeout_seconds, 6.0),
            )
            if await self._is_login_required(page):
                await self._open_login_prompt(page)
                await self._capture_login_screenshot(page)

    async def health(self) -> dict[str, Any]:
        await self.start()
        ready = False
        current_url = None
        login_required = False
        try:
            async with self._request_lock:
                page = await self._open_page()
                await self._wait_for_page_ready(
                    page,
                    timeout=self._config.ready_timeout_seconds,
                )
                current_url = page.url
                ready = await self._has_ready_composer(page, timeout=3.0)
                login_required = await self._is_login_required(page)
                if login_required:
                    await self._open_login_prompt(page)
                    await self._capture_login_screenshot(page)
        except Exception as exc:
            return {"ok": False, "ready": False, "error": str(exc)}

        return {
            "ok": True,
            "ready": ready and not login_required,
            "login_required": login_required,
            "url": current_url,
            "login_screenshot": "/login/screenshot" if login_required else None,
        }

    async def prepare_login_screenshot(self) -> Path:
        await self.start()
        async with self._request_lock:
            assert self._context is not None
            page = await self._context.new_page()
            try:
                await page.goto(self._config.chat_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(6000)
                await self._wait_for_page_ready(
                    page,
                    timeout=max(self._config.ready_timeout_seconds, 6.0),
                )
                await self._open_login_prompt(page)
                await page.wait_for_timeout(4000)
                await self._capture_login_screenshot(page)
            finally:
                await page.close()
        return self._config.login_screenshot_path

    async def complete_chat(self, *, prompt: str) -> str:
        await self.start()
        async with self._request_lock:
            page = await self._open_page()
            await self._prepare_for_new_request(page)
            before = await self._snapshot(page)
            try:
                composer = await self._require_composer(page)
                await self._submit_prompt(page, composer, prompt)
                content = await self._wait_for_response(page, before, prompt)
            except BridgeExecutionError as exc:
                raise await self._enrich_bridge_error(exc, page) from exc
        return content

    async def _open_page(self) -> Any:
        assert self._context is not None
        candidate_page = self._page
        if candidate_page is None or candidate_page.is_closed():
            candidate_page = None
            for existing_page in reversed(self._context.pages):
                if existing_page.is_closed():
                    continue
                if "doubao.com" in (existing_page.url or ""):
                    candidate_page = existing_page
                    break
                if candidate_page is None:
                    candidate_page = existing_page

        if candidate_page is None:
            candidate_page = await self._context.new_page()

        self._page = candidate_page
        page = candidate_page

        if not page.url or "doubao.com" not in page.url:
            await page.goto(self._config.chat_url, wait_until="domcontentloaded")

        if not self._config.headless:
            with contextlib.suppress(Exception):
                await page.bring_to_front()
            with contextlib.suppress(Exception):
                await page.evaluate("() => { window.focus?.(); }")
        return page

    async def _has_ready_composer(self, page: Any, *, timeout: float) -> bool:
        try:
            await self._find_composer(page, timeout=timeout)
        except BridgeExecutionError:
            return False
        return True

    async def _wait_for_page_ready(self, page: Any, *, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "doubao-region-ban" in page.url:
                raise BridgeExecutionError(
                    "Doubao web blocked this environment with the region-ban page.",
                    status_code=503,
                    code="doubao_region_ban",
                )
            try:
                body = await self._body_text(page)
            except Exception as exc:
                lowered = str(exc).lower()
                if (
                    "execution context was destroyed" in lowered
                    or "navigation" in lowered
                ):
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state(
                            "domcontentloaded",
                            timeout=5000,
                        )
                    continue
                raise
            if body:
                return body
            await page.wait_for_timeout(250)
        raise BridgeExecutionError(
            "Doubao page did not finish rendering in time.",
            status_code=503,
            code="doubao_page_not_ready",
        )

    async def _prepare_for_new_request(self, page: Any) -> None:
        await self._wait_for_page_ready(
            page,
            timeout=self._config.ready_timeout_seconds,
        )
        await self._try_click_new_chat(page)
        await page.wait_for_timeout(600)
        await self._wait_for_page_ready(page, timeout=10.0)

    async def _try_click_new_chat(self, page: Any) -> None:
        for selector in self._config.new_chat_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    await locator.click(force=True, timeout=1200)
                    return
            except Exception:
                continue

        for label in self._config.new_chat_labels:
            pattern = re.compile(re.escape(label))
            for locator_factory in (
                lambda: page.get_by_role("button", name=pattern),
                lambda: page.get_by_text(pattern),
            ):
                try:
                    locator = locator_factory().first
                    if await locator.count():
                        await locator.click(force=True, timeout=1200)
                        return
                except Exception:
                    continue

    async def _open_login_prompt(self, page: Any) -> None:
        candidate_locators = (
            page.get_by_role("button", name=re.compile(f"^{LOGIN_BUTTON_TEXT}$")).first,
            page.get_by_text(LOGIN_BUTTON_TEXT, exact=True).first,
            page.get_by_text(LOGIN_BUTTON_TEXT).first,
        )
        for login_button in candidate_locators:
            try:
                if await login_button.count() == 0:
                    continue
                if not await login_button.is_visible():
                    continue
                await login_button.click(force=True, timeout=1200)
                await page.wait_for_timeout(1500)
                if await self._is_login_required(page):
                    return
            except Exception:
                continue

    async def _require_composer(self, page: Any) -> Any:
        try:
            return await self._find_composer(
                page,
                timeout=self._config.ready_timeout_seconds,
            )
        except BridgeExecutionError as exc:
            page_text = await self._body_text(page)
            if "登录" in page_text or "扫码" in page_text:
                raise BridgeExecutionError(
                    "Doubao web is not ready. Log into doubao.com in the configured browser profile first.",
                    status_code=503,
                    code="doubao_login_required",
                ) from exc
            raise

    async def _find_composer(self, page: Any, *, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for selector in self._config.input_selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() == 0:
                        continue
                    if await locator.is_visible():
                        return locator
                except Exception:
                    continue
            await page.wait_for_timeout(250)
        raise BridgeExecutionError(
            "Doubao composer not found. Open the browser profile and ensure the chat page is accessible.",
            status_code=503,
            code="doubao_composer_not_found",
        )

    async def _is_login_required(self, page: Any) -> bool:
        if "from_logout=1" in page.url:
            return True
        body = await self._body_text(page)
        return any(hint in body for hint in LOGIN_HINTS)

    async def _capture_login_screenshot(self, page: Any) -> None:
        self._config.login_screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(
            path=str(self._config.login_screenshot_path),
            full_page=True,
        )

    async def _raise_login_required(self, page: Any) -> None:
        await self._open_login_prompt(page)
        await self._capture_login_screenshot(page)
        raise BridgeExecutionError(
            "Doubao web requires login before it can answer. Open /login/screenshot and scan with the Doubao app or complete phone login.",
            status_code=503,
            code="doubao_login_required",
        )

    async def _submit_prompt(self, page: Any, composer: Any, prompt: str) -> None:
        await composer.click()
        tag_name = (
            await composer.evaluate("(node) => node.tagName.toLowerCase()")
        ).strip()
        if tag_name == "textarea":
            await composer.fill(prompt)
        else:
            await page.keyboard.press("Control+A")
            await page.keyboard.type(prompt)
        await composer.press("Enter")

    async def _wait_for_response(
        self,
        page: Any,
        before: ChatSnapshot,
        prompt: str,
    ) -> str:
        deadline = time.monotonic() + self._config.timeout_seconds
        best_candidate = ""
        stable_polls = 0
        last_snapshot = before
        while time.monotonic() < deadline:
            await page.wait_for_timeout(900)
            if "from_logout=1" in page.url:
                await self._raise_login_required(page)
            try:
                if await self._is_login_required(page):
                    await self._raise_login_required(page)
                after = await self._snapshot(page)
            except Exception as exc:
                lowered = str(exc).lower()
                if (
                    "execution context was destroyed" in lowered
                    or "navigation" in lowered
                ):
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    if "from_logout=1" in page.url:
                        await self._raise_login_required(page)
                    continue
                raise
            last_snapshot = after
            if _looks_like_home_screen(after.body_text):
                continue
            candidate = derive_response_text(before=before, after=after, prompt=prompt)
            if candidate and candidate == best_candidate:
                stable_polls += 1
                if stable_polls >= 2:
                    return candidate
            elif candidate:
                best_candidate = candidate
                stable_polls = 1

        if best_candidate:
            return best_candidate

        details = await self._collect_failure_details(page, snapshot=last_snapshot)
        if details.get("captcha_detected"):
            raise BridgeExecutionError(
                "Doubao web is blocked by a captcha challenge. Complete the verification in the browser window and retry.",
                status_code=502,
                code="doubao_captcha_required",
                details=details,
            )
        raise BridgeExecutionError(
            f"No usable Doubao reply was captured within {self._config.timeout_seconds:.0f} seconds.",
            status_code=504,
            code="doubao_timeout",
            details=details,
        )

    async def _has_captcha_challenge(self, page: Any) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """
                    () => Boolean(
                      document.querySelector('#captcha_container iframe') ||
                      document.querySelector('iframe[src*="verifycenter/captcha"]') ||
                      document.querySelector('[id*="captcha"] iframe')
                    )
                    """
                )
            )
        except Exception:
            return False

    async def _collect_failure_details(
        self,
        page: Any,
        *,
        snapshot: ChatSnapshot | None = None,
    ) -> dict[str, Any]:
        page_url = ""
        with contextlib.suppress(Exception):
            page_url = str(page.url or "")

        current_snapshot = snapshot
        if current_snapshot is None:
            with contextlib.suppress(Exception):
                current_snapshot = await self._snapshot(page)

        return {
            "page_url": page_url,
            "captcha_detected": await self._has_captcha_challenge(page),
            "body_text": current_snapshot.body_text
            if current_snapshot is not None
            else "",
            "blocks": list(current_snapshot.blocks)
            if current_snapshot is not None
            else [],
        }

    async def _enrich_bridge_error(
        self,
        exc: BridgeExecutionError,
        page: Any,
    ) -> BridgeExecutionError:
        details = exc.details or await self._collect_failure_details(page)
        if details.get("captcha_detected") and exc.code != "doubao_captcha_required":
            return BridgeExecutionError(
                "Doubao web is blocked by a captcha challenge. Complete the verification in the browser window and retry.",
                status_code=502,
                code="doubao_captcha_required",
                details=details,
            )
        return BridgeExecutionError(
            str(exc),
            status_code=exc.status_code,
            code=exc.code,
            details=details,
        )

    async def _snapshot(self, page: Any) -> ChatSnapshot:
        data = await page.evaluate(
            """
            () => {
              const root =
                document.querySelector('main') ||
                document.querySelector('[role="main"]') ||
                document.body;
              const clean = (text) =>
                (text || '')
                  .replace(/\\u00a0/g, ' ')
                  .replace(/[ \\t]+\\n/g, '\\n')
                  .replace(/\\n{3,}/g, '\\n\\n')
                  .trim();
              const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return (
                  style &&
                  style.display !== 'none' &&
                  style.visibility !== 'hidden' &&
                  rect.width > 0 &&
                  rect.height > 0
                );
              };

              const blocks = [];
              const nodes = root.querySelectorAll(
                'article, section, p, pre, li, div, span'
              );
              for (const el of nodes) {
                if (!isVisible(el)) {
                  continue;
                }
                const text = clean(el.innerText);
                if (!text || text.length < 2 || text.length > 4000) {
                  continue;
                }
                let sameChild = false;
                for (const child of Array.from(el.children)) {
                  if (clean(child.innerText) === text) {
                    sameChild = true;
                    break;
                  }
                }
                if (!sameChild) {
                  blocks.push(text);
                }
              }

              return {
                body_text: clean(root.innerText),
                blocks: Array.from(new Set(blocks)).slice(-80),
              };
            }
            """
        )
        body_text = _normalize_text(str(data.get("body_text", "")))
        blocks = tuple(
            _normalize_text(str(block))
            for block in data.get("blocks", [])
            if _normalize_text(str(block))
        )
        return ChatSnapshot(body_text=body_text, blocks=blocks)

    async def _body_text(self, page: Any) -> str:
        text = await page.evaluate(
            """
            () => {
              const root =
                document.querySelector('main') ||
                document.querySelector('[role="main"]') ||
                document.body;
              return (root.innerText || '').trim();
            }
            """
        )
        return _normalize_text(str(text))


def create_app(config: BridgeConfig) -> Quart:
    app = Quart(__name__)
    semaphore = asyncio.Semaphore(config.max_parallel_requests)
    bridge = DoubaoBrowserBridge(config)

    @app.before_serving
    async def before_serving() -> None:
        await bridge.start()
        try:
            await bridge.warm_up()
        except Exception as exc:
            LOGGER.warning("Doubao bridge warm-up failed: %s", exc)

    @app.after_serving
    async def after_serving() -> None:
        await bridge.stop()

    @app.get("/")
    async def index() -> Response:
        return jsonify(
            {
                "name": "doubao-web-bridge",
                "models": list(config.advertised_models),
                "chat_url": config.chat_url,
                "user_data_dir": str(config.user_data_dir),
            }
        )

    @app.get("/healthz")
    async def healthz() -> Response | tuple[Response, int]:
        if auth_error := _require_auth(config):
            return auth_error
        return jsonify(await bridge.health())

    @app.get("/login/screenshot")
    async def login_screenshot() -> Response | tuple[Response, int]:
        if auth_error := _require_auth(config):
            return auth_error
        screenshot_path = await bridge.prepare_login_screenshot()
        response = await send_file(
            screenshot_path,
            mimetype="image/png",
            conditional=False,
        )
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/models")
    @app.get("/v1/models")
    async def list_models() -> Response | tuple[Response, int]:
        if auth_error := _require_auth(config):
            return auth_error
        return jsonify(
            {
                "object": "list",
                "data": [
                    _model_payload(model_id) for model_id in config.advertised_models
                ],
            }
        )

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

        requested_model = str(payload.get("model") or config.default_model)
        raw_prompt = payload.get("raw_prompt")
        if isinstance(raw_prompt, str) and raw_prompt.strip():
            prompt = raw_prompt.strip()
            prompt_message_count = 0
        else:
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

            prompt = build_chat_prompt(normalized_messages)
            prompt_message_count = len(normalized_messages)
            if not prompt:
                return _build_error_response(
                    "The supplied messages did not contain any usable text.",
                    status_code=400,
                    code="empty_prompt",
                )

        LOGGER.info(
            "Forwarding chat completion to Doubao Web. model=%s messages=%s stream=%s",
            requested_model,
            prompt_message_count,
            bool(payload.get("stream")),
        )
        _capture_prompt(
            config.prompt_capture_path,
            prompt=prompt,
            requested_model=requested_model,
            message_count=prompt_message_count,
        )

        queued_at = time.monotonic()
        try:
            async with semaphore:
                wait_seconds = time.monotonic() - queued_at
                started_at = time.monotonic()
                content = await bridge.complete_chat(prompt=prompt)
        except BridgeExecutionError as exc:
            _capture_incident(
                config.incident_capture_dir,
                prompt=prompt,
                requested_model=requested_model,
                message_count=prompt_message_count,
                status_code=exc.status_code,
                code=exc.code,
                error_message=str(exc),
                details=exc.details,
            )
            return _build_error_response(
                str(exc),
                status_code=exc.status_code,
                code=exc.code,
            )
        except Exception as exc:
            LOGGER.exception("Doubao web bridge failed.")
            _capture_incident(
                config.incident_capture_dir,
                prompt=prompt,
                requested_model=requested_model,
                message_count=prompt_message_count,
                status_code=502,
                code="doubao_bridge_failed",
                error_message=str(exc),
                details={"exception_type": type(exc).__name__},
            )
            return _build_error_response(
                str(exc),
                status_code=502,
                code="doubao_bridge_failed",
            )

        LOGGER.info(
            "Completed chat completion via Doubao Web. model=%s messages=%s wait_s=%.2f run_s=%.2f",
            requested_model,
            prompt_message_count,
            wait_seconds,
            time.monotonic() - started_at,
        )

        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        if payload.get("stream"):

            async def stream_events() -> Any:
                for item in build_stream_chunk_payloads(
                    content=content,
                    model=requested_model,
                    completion_id=completion_id,
                    created=created,
                    chunk_size=config.stream_chunk_chars,
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
            )
        )

    return app


def parse_args(argv: list[str] | None = None) -> BridgeConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Expose a local OpenAI-compatible chat endpoint backed by "
            "the Doubao web app."
        )
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("DOUBAO_BRIDGE_HOST", "127.0.0.1"),
        help="Bind address for the local bridge server.",
    )
    parser.add_argument(
        "--port",
        default=int(os.environ.get("DOUBAO_BRIDGE_PORT", "8790")),
        type=int,
        help="Bind port for the local bridge server.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DOUBAO_BRIDGE_MODEL", DEFAULT_MODEL),
        help="Default model name to advertise and use when omitted by callers.",
    )
    parser.add_argument(
        "--advertise-model",
        action="append",
        default=[],
        help="Additional model IDs to expose from /v1/models.",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("DOUBAO_BRIDGE_AUTH_TOKEN") or None,
        help="Optional bearer token required by the bridge.",
    )
    parser.add_argument(
        "--stream-chunk-chars",
        default=int(os.environ.get("DOUBAO_BRIDGE_STREAM_CHUNK_CHARS", "64")),
        type=int,
        help="Pseudo-stream chunk size in characters for stream=true requests.",
    )
    parser.add_argument(
        "--max-parallel-requests",
        default=int(os.environ.get("DOUBAO_BRIDGE_MAX_PARALLEL", "1")),
        type=int,
        help="Maximum number of concurrent browser requests.",
    )
    parser.add_argument(
        "--timeout",
        default=float(os.environ.get("DOUBAO_BRIDGE_TIMEOUT", "120")),
        type=float,
        help="Maximum time to wait for a Doubao response, in seconds.",
    )
    parser.add_argument(
        "--ready-timeout",
        default=float(os.environ.get("DOUBAO_BRIDGE_READY_TIMEOUT", "20")),
        type=float,
        help="Maximum time to wait for the Doubao composer to appear.",
    )
    parser.add_argument(
        "--browser-executable",
        default=os.environ.get("DOUBAO_BRIDGE_BROWSER") or None,
        help="Optional browser executable path. Defaults to Playwright's chromium channel detection.",
    )
    parser.add_argument(
        "--user-data-dir",
        default=os.environ.get(
            "DOUBAO_BRIDGE_USER_DATA_DIR",
            str(Path(os.getcwd()) / "runtime" / "doubao-prod" / "browser-profile"),
        ),
        help="Persistent browser user data directory used for the Doubao login state.",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("DOUBAO_BRIDGE_HEADLESS", "false").lower() == "true",
        help="Run the browser headlessly. Use --no-headless for interactive login sessions.",
    )
    parser.add_argument(
        "--chat-url",
        default=os.environ.get("DOUBAO_BRIDGE_CHAT_URL", DEFAULT_CHAT_URL),
        help="Doubao chat URL to open for each request.",
    )
    parser.add_argument(
        "--input-selector",
        action="append",
        default=[],
        help="Repeatable CSS selector candidates for the chat composer.",
    )
    parser.add_argument(
        "--new-chat-label",
        action="append",
        default=[],
        help="Repeatable visible labels used to try starting a fresh chat.",
    )
    parser.add_argument(
        "--new-chat-selector",
        action="append",
        default=[],
        help="Repeatable CSS selectors used before label heuristics to start a fresh chat.",
    )
    parser.add_argument(
        "--prompt-capture-path",
        default=os.environ.get("DOUBAO_BRIDGE_PROMPT_CAPTURE_PATH") or None,
        help="Optional JSONL file path used to append raw prompts forwarded to the Doubao web page.",
    )
    parser.add_argument(
        "--incident-capture-dir",
        default=os.environ.get("DOUBAO_BRIDGE_INCIDENT_CAPTURE_DIR") or None,
        help="Optional directory used to write failure bundles when the Doubao web page hits captcha, timeout, or bridge errors.",
    )

    args = parser.parse_args(argv)

    advertised_models = tuple(
        dict.fromkeys([args.model, *[model for model in args.advertise_model if model]])
    )
    user_data_dir = Path(args.user_data_dir).expanduser().resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    login_screenshot_path = user_data_dir.parent / SCREENSHOT_FILENAME
    prompt_capture_path = (
        Path(args.prompt_capture_path).expanduser().resolve()
        if args.prompt_capture_path
        else None
    )
    incident_capture_dir = (
        Path(args.incident_capture_dir).expanduser().resolve()
        if args.incident_capture_dir
        else None
    )

    input_selectors = tuple(
        dict.fromkeys(
            [*DEFAULT_INPUT_SELECTORS, *[item for item in args.input_selector if item]]
        )
    )
    new_chat_labels = tuple(
        dict.fromkeys(
            [*DEFAULT_NEW_CHAT_LABELS, *[item for item in args.new_chat_label if item]]
        )
    )
    new_chat_selectors = tuple(
        dict.fromkeys(
            [
                *DEFAULT_NEW_CHAT_SELECTORS,
                *[item for item in args.new_chat_selector if item],
            ]
        )
    )

    return BridgeConfig(
        host=args.host,
        port=args.port,
        default_model=args.model,
        advertised_models=advertised_models,
        auth_token=args.auth_token,
        stream_chunk_chars=max(args.stream_chunk_chars, 1),
        max_parallel_requests=max(args.max_parallel_requests, 1),
        timeout_seconds=max(args.timeout, 5),
        ready_timeout_seconds=max(args.ready_timeout, 3),
        browser_executable=args.browser_executable,
        user_data_dir=user_data_dir,
        login_screenshot_path=login_screenshot_path,
        headless=args.headless,
        chat_url=args.chat_url,
        input_selectors=input_selectors,
        new_chat_labels=new_chat_labels,
        new_chat_selectors=new_chat_selectors,
        prompt_capture_path=prompt_capture_path,
        incident_capture_dir=incident_capture_dir,
    )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = parse_args(argv)
    app = create_app(config)
    LOGGER.info(
        "Starting Doubao web bridge on http://%s:%s (model=%s, user_data_dir=%s, headless=%s)",
        config.host,
        config.port,
        config.default_model,
        config.user_data_dir,
        config.headless,
    )
    app.run(host=config.host, port=config.port)


if __name__ == "__main__":
    main()
