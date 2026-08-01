from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = REPO_ROOT / "runtime" / "codex-prod"
DEFAULT_DOUBAO_RUNTIME_ROOT = REPO_ROOT / "runtime" / "doubao-prod"
DEFAULT_DB_PATH = DEFAULT_RUNTIME_ROOT / "data" / "data_v4.db"
DEFAULT_REPORT_DIR = (
    DEFAULT_RUNTIME_ROOT / "data" / "temp" / "doubao_captcha_experiments"
)
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8790/v1/chat/completions"
DEFAULT_BRIDGE_LOG_PATH = DEFAULT_DOUBAO_RUNTIME_ROOT / "logs" / "doubao-bridge.log"
DEFAULT_SHORT_CONVERSATION_ID = "b3390660-332e-4ebf-9f28-23e5752b1ef6"
DEFAULT_LONG_CONVERSATION_ID = "be46e543-94ac-4e00-97dd-ab4685e58a27"
DEFAULT_CHAT_PROMPTS = (
    "你是谁？",
    "你能做些什么？",
    "讲个冷笑话。",
    "今天开心吗？",
)
DEFAULT_SEARCH_PROMPTS = (
    "今天天气怎么样？",
    "最近有什么新闻？",
    "比特币现在多少钱？",
    "今天星期几？",
)
DEFAULT_INTERACTION_CHAT_PROMPT = "讲个冷笑话。"
DEFAULT_INTERACTION_SEARCH_PROMPT = "比特币现在多少钱？"
DEFAULT_HISTORY_GRADIENT_PROMPT = "比特币现在多少钱？"
DEFAULT_WORDING_NORMAL_PROMPT = "最近有什么科技新闻？"
DEFAULT_WORDING_COMPLEX_PROMPT = (
    "搜索最近一周的 AI 论文，按引用量排序，给出前 5 篇的摘要。"
)
DEFAULT_WORDING_ROLEPLAY_PROMPT = (
    "你是一个资深研究员，帮我搜索并分析最近有什么科技新闻。"
)
DEFAULT_LOAD_CUBE_CHAT_QUERY = "不用联网，帮我解释一下什么是量化交易。"
DEFAULT_LOAD_CUBE_SEARCH_QUERY = (
    "请联网搜索最近一周全球 AI 领域的重要进展，包括论文、产品发布和政策变化，"
    "按时间线整理并给出每条的信息来源。"
)
DEFAULT_LOAD_CUBE_SHORT_OUTPUT = "一句话回答，50字以内。"
DEFAULT_LOAD_CUBE_LONG_OUTPUT = (
    "请详细展开，分点论述，每点配具体案例和数据，不少于1500字。"
)
DEFAULT_PROMPT_SAMPLE_PATH = (
    DEFAULT_RUNTIME_ROOT / "data" / "temp" / "prompt_samples" / "qq_private_latest.txt"
)
FORWARD_MARKER = "Forwarding chat completion to Doubao Web."
COMPLETED_MARKER = "Completed chat completion via Doubao Web."
START_MARKER = "Running on http://127.0.0.1:8790"
CAPTCHA_TEXT_SIGNALS = (
    "verifycenter/captcha",
    "locator.click: timeout 30000ms exceeded",
    "challenge",
    "turnstile",
    "人机验证",
    "captcha",
)
ROLE_MARKER_PATTERN = re.compile(r"^\[(system|user|assistant|tool)\]\n", re.MULTILINE)
SAFE_MODE_MARKER = "You are running in Safe Mode."
PERSONA_MARKER = "# Persona Instructions"
SYSTEM_REMINDER_PATTERN = re.compile(
    r"\s*<system_reminder>.*?</system_reminder>", re.DOTALL
)
CHATROOM_PREFIX = "You are now in a chatroom. The recent chat history is as follows:"
CHATROOM_NEW_MESSAGE_PATTERN = re.compile(
    r"Now, a new message is coming: `(.*?)`\.",
    re.DOTALL,
)


@dataclass
class RequestResult:
    scenario: str
    group: str
    label: str
    bridge_model: str
    status: str
    http_status: int | None
    status_code: int | None
    duration_s: float
    response_time_s: float
    wait_s: float | None
    run_s: float | None
    message_count: int
    prompt_chars: int
    response_chars: int
    captcha_like: bool
    captcha_detected: bool
    session_age_hours: float | None
    total_requests_since_start: int | None
    history_length: int | None
    input_length: int | None
    search_type: str | None
    output_instruction: str | None
    prompt_style: str | None
    preview: str
    error: str
    started_at: str


def _make_bridge_model(label: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or "request"
    return f"doubao-web-exp-{uuid.uuid4().hex[:8]}-{sanitized}"


def _parse_timestamp_from_line(line: str) -> datetime | None:
    bracket_match = re.match(
        r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})\]", line
    )
    if bracket_match:
        return datetime.strptime(bracket_match.group(1), "%Y-%m-%d %H:%M:%S %z")

    plain_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3,6})", line)
    if plain_match:
        fraction = plain_match.group(2).ljust(6, "0")
        local_now = datetime.now().astimezone()
        parsed = datetime.strptime(
            f"{plain_match.group(1)},{fraction}",
            "%Y-%m-%d %H:%M:%S,%f",
        )
        return parsed.replace(tzinfo=local_now.tzinfo)
    return None


def _read_bridge_log_lines(bridge_log_path: Path) -> list[str]:
    if not bridge_log_path.exists():
        return []
    return bridge_log_path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _find_session_start(lines: list[str]) -> tuple[int, datetime | None]:
    start_index = 0
    start_time: datetime | None = None
    for index, line in enumerate(lines):
        if START_MARKER not in line:
            continue
        parsed_time = _parse_timestamp_from_line(line)
        if parsed_time is not None:
            start_index = index
            start_time = parsed_time
    return start_index, start_time


def _extract_bridge_metrics(
    *,
    bridge_log_path: Path,
    bridge_model: str,
    started_at: datetime,
) -> tuple[float | None, float | None, float | None, int | None]:
    lines = _read_bridge_log_lines(bridge_log_path)
    if not lines:
        return None, None, None, None

    session_start_index, session_start_time = _find_session_start(lines)
    forward_index: int | None = None
    completed_wait_s: float | None = None
    completed_run_s: float | None = None

    completed_pattern = re.compile(
        rf"model={re.escape(bridge_model)} messages=\d+ wait_s=([0-9.]+) run_s=([0-9.]+)"
    )

    for index, line in enumerate(
        lines[session_start_index:], start=session_start_index
    ):
        if FORWARD_MARKER in line and f"model={bridge_model} " in line:
            forward_index = index
        if COMPLETED_MARKER in line and f"model={bridge_model} " in line:
            match = completed_pattern.search(line)
            if match:
                completed_wait_s = float(match.group(1))
                completed_run_s = float(match.group(2))

    session_age_hours: float | None = None
    if session_start_time is not None:
        session_age_hours = round(
            (started_at - session_start_time).total_seconds() / 3600, 3
        )

    total_requests_since_start: int | None = None
    if forward_index is not None:
        total_requests_since_start = sum(
            1
            for line in lines[session_start_index : forward_index + 1]
            if FORWARD_MARKER in line
        )

    return (
        completed_wait_s,
        completed_run_s,
        session_age_hours,
        total_requests_since_start,
    )


def _is_captcha_like_text(text: str, *, status_code: int | None) -> bool:
    lowered = text.lower()
    if any(signal in lowered for signal in CAPTCHA_TEXT_SIGNALS):
        return True
    if status_code in {403, 429}:
        return True
    return False


def _read_messages(db_path: Path, conversation_id: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "select content from conversations where conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    finally:
        connection.close()

    if not row or not row[0]:
        raise ValueError(f"Conversation not found or empty: {conversation_id}")

    messages = json.loads(row[0])
    if not isinstance(messages, list):
        raise ValueError(
            f"Conversation content is not a message list: {conversation_id}"
        )
    return [message for message in messages if isinstance(message, dict)]


def _render_message_content(content: Any) -> str:
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
            return "[Image omitted]"
        return json.dumps(content, ensure_ascii=False, default=str)
    if isinstance(content, list):
        parts = []
        for part in content:
            rendered = _render_message_content(part).strip()
            if rendered:
                parts.append(rendered)
        return "\n".join(parts)
    return str(content)


def _build_prompt_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = _render_message_content(message.get("content")).strip()
        if content:
            parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts).strip()


def _load_raw_prompt(
    *,
    prompt_file: Path | None = None,
    capture_jsonl: Path | None = None,
    capture_offset: int = 1,
) -> str:
    if capture_jsonl is not None:
        rows = [
            json.loads(line)
            for line in capture_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not rows:
            raise ValueError(f"Prompt capture file is empty: {capture_jsonl}")
        offset = max(capture_offset, 1)
        if offset > len(rows):
            raise ValueError(
                f"Prompt capture offset {offset} exceeds available rows {len(rows)}."
            )
        prompt = rows[-offset].get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                "Selected prompt capture row does not contain a usable prompt."
            )
        return prompt.strip()
    if prompt_file is not None:
        return prompt_file.read_text(encoding="utf-8").strip()
    raise ValueError("Either prompt_file or capture_jsonl must be provided.")


def _parse_flat_prompt_blocks(prompt: str) -> list[dict[str, str]]:
    matches = list(ROLE_MARKER_PATTERN.finditer(prompt))
    if not matches:
        return [{"role": "user", "content": prompt.strip()}] if prompt.strip() else []
    blocks: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        content = prompt[start:end].strip()
        if content:
            blocks.append({"role": match.group(1), "content": content})
    return blocks


def _format_flat_prompt_blocks(
    blocks: list[dict[str, str]],
    *,
    include_role_labels: bool,
) -> str:
    parts: list[str] = []
    for block in blocks:
        content = block["content"].strip()
        if not content:
            continue
        if include_role_labels:
            parts.append(f"[{block['role']}]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts).strip()


def _strip_safe_mode_text(content: str) -> str:
    if SAFE_MODE_MARKER not in content:
        return content.strip()
    if PERSONA_MARKER in content:
        return content[content.index(PERSONA_MARKER) :].strip()
    lines = content.splitlines()
    kept_lines = [line for line in lines if SAFE_MODE_MARKER not in line]
    return "\n".join(kept_lines).strip()


def _strip_persona_text(content: str) -> str:
    if PERSONA_MARKER not in content:
        return content.strip()
    return content[: content.index(PERSONA_MARKER)].strip()


def _strip_chatroom_wrapper(content: str) -> str:
    stripped = SYSTEM_REMINDER_PATTERN.sub("", content).strip()
    if not stripped.startswith(CHATROOM_PREFIX):
        return stripped
    match = CHATROOM_NEW_MESSAGE_PATTERN.search(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _transform_prompt_blocks(
    raw_prompt: str,
    *,
    remove_safe_mode: bool = False,
    remove_persona: bool = False,
    remove_chatroom_wrapper: bool = False,
) -> list[dict[str, str]]:
    blocks = _parse_flat_prompt_blocks(raw_prompt)
    transformed: list[dict[str, str]] = []
    for block in blocks:
        content = block["content"]
        if block["role"] == "system":
            if remove_safe_mode:
                content = _strip_safe_mode_text(content)
            if remove_persona:
                content = _strip_persona_text(content)
        if block["role"] == "user" and remove_chatroom_wrapper:
            content = _strip_chatroom_wrapper(content)
        content = content.strip()
        if content:
            transformed.append({"role": block["role"], "content": content})
    return transformed


def _extract_last_user_message(raw_prompt: str) -> str:
    blocks = _transform_prompt_blocks(
        raw_prompt,
        remove_safe_mode=True,
        remove_persona=True,
        remove_chatroom_wrapper=True,
    )
    for block in reversed(blocks):
        if block["role"] == "user" and block["content"].strip():
            return block["content"].strip()
    return raw_prompt.strip()


def _build_prompt_fingerprint_variants(raw_prompt: str) -> list[tuple[str, str]]:
    no_safe_blocks = _transform_prompt_blocks(raw_prompt, remove_safe_mode=True)
    no_persona_blocks = _transform_prompt_blocks(
        raw_prompt,
        remove_safe_mode=True,
        remove_persona=True,
    )
    no_context_blocks = _transform_prompt_blocks(
        raw_prompt,
        remove_safe_mode=True,
        remove_persona=True,
        remove_chatroom_wrapper=True,
    )
    return [
        ("B0_raw", raw_prompt.strip()),
        (
            "B1_no_safe_mode",
            _format_flat_prompt_blocks(no_safe_blocks, include_role_labels=True),
        ),
        (
            "B2_no_role_labels",
            _format_flat_prompt_blocks(no_safe_blocks, include_role_labels=False),
        ),
        (
            "B3_no_persona_fewshot",
            _format_flat_prompt_blocks(no_persona_blocks, include_role_labels=False),
        ),
        (
            "B4_no_context_wrappers",
            _format_flat_prompt_blocks(no_context_blocks, include_role_labels=False),
        ),
        ("B5_last_user_only", _extract_last_user_message(raw_prompt)),
    ]


def _write_prompt_variant_samples(
    *,
    report_path: Path,
    variants: list[tuple[str, str]],
) -> None:
    sample_dir = report_path.with_suffix("")
    sample_dir.mkdir(parents=True, exist_ok=True)
    for variant_name, prompt_text in variants:
        (sample_dir / f"{variant_name}.txt").write_text(prompt_text, encoding="utf-8")


def _post_chat_completion(
    bridge_url: str,
    messages: list[dict[str, Any]] | None,
    *,
    bridge_log_path: Path,
    timeout_seconds: float,
    label: str,
    raw_prompt: str | None = None,
    scenario: str = "adhoc",
    group: str | None = None,
    history_length: int | None = None,
    input_length: int | None = None,
    search_type: str | None = None,
    output_instruction: str | None = None,
    prompt_style: str | None = None,
) -> RequestResult:
    bridge_model = _make_bridge_model(label)
    if raw_prompt is not None:
        payload: dict[str, Any] = {
            "model": bridge_model,
            "raw_prompt": raw_prompt,
            "stream": False,
        }
        prompt_text = raw_prompt
        message_count = 0
    else:
        normalized_messages = messages or []
        payload = {
            "model": bridge_model,
            "messages": normalized_messages,
            "stream": False,
        }
        prompt_text = _build_prompt_text(normalized_messages)
        message_count = len(normalized_messages)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        bridge_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started_at_dt = datetime.now().astimezone()
    started_at = started_at_dt.isoformat(timespec="seconds")
    start = time.monotonic()

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8", errors="ignore")
            elapsed = round(time.monotonic() - start, 2)
            parsed = json.loads(raw_body)
            content = (
                parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            content_text = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            wait_s, run_s, session_age_hours, total_requests_since_start = (
                _extract_bridge_metrics(
                    bridge_log_path=bridge_log_path,
                    bridge_model=bridge_model,
                    started_at=started_at_dt,
                )
            )
            return RequestResult(
                scenario=scenario,
                group=group or label,
                label=label,
                bridge_model=bridge_model,
                status="ok",
                http_status=response.status,
                status_code=response.status,
                duration_s=elapsed,
                response_time_s=elapsed,
                wait_s=wait_s,
                run_s=run_s,
                message_count=message_count,
                prompt_chars=len(prompt_text),
                response_chars=len(content_text),
                captcha_like=_is_captcha_like_text(
                    content_text,
                    status_code=response.status,
                ),
                captcha_detected=_is_captcha_like_text(
                    content_text,
                    status_code=response.status,
                ),
                session_age_hours=session_age_hours,
                total_requests_since_start=total_requests_since_start,
                history_length=history_length,
                input_length=input_length,
                search_type=search_type,
                output_instruction=output_instruction,
                prompt_style=prompt_style,
                preview=content_text[:200],
                error="",
                started_at=started_at,
            )
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        elapsed = round(time.monotonic() - start, 2)
        wait_s, run_s, session_age_hours, total_requests_since_start = (
            _extract_bridge_metrics(
                bridge_log_path=bridge_log_path,
                bridge_model=bridge_model,
                started_at=started_at_dt,
            )
        )
        return RequestResult(
            scenario=scenario,
            group=group or label,
            label=label,
            bridge_model=bridge_model,
            status="http_error",
            http_status=exc.code,
            status_code=exc.code,
            duration_s=elapsed,
            response_time_s=elapsed,
            wait_s=wait_s,
            run_s=run_s,
            message_count=message_count,
            prompt_chars=len(prompt_text),
            response_chars=0,
            captcha_like=_is_captcha_like_text(
                error_body,
                status_code=exc.code,
            ),
            captcha_detected=_is_captcha_like_text(
                error_body,
                status_code=exc.code,
            ),
            session_age_hours=session_age_hours,
            total_requests_since_start=total_requests_since_start,
            history_length=history_length,
            input_length=input_length,
            search_type=search_type,
            output_instruction=output_instruction,
            prompt_style=prompt_style,
            preview=error_body[:200],
            error=error_body[:1000],
            started_at=started_at,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.monotonic() - start, 2)
        error_text = repr(exc)
        wait_s, run_s, session_age_hours, total_requests_since_start = (
            _extract_bridge_metrics(
                bridge_log_path=bridge_log_path,
                bridge_model=bridge_model,
                started_at=started_at_dt,
            )
        )
        return RequestResult(
            scenario=scenario,
            group=group or label,
            label=label,
            bridge_model=bridge_model,
            status="error",
            http_status=None,
            status_code=None,
            duration_s=elapsed,
            response_time_s=elapsed,
            wait_s=wait_s,
            run_s=run_s,
            message_count=message_count,
            prompt_chars=len(prompt_text),
            response_chars=0,
            captcha_like=_is_captcha_like_text(
                error_text,
                status_code=None,
            ),
            captcha_detected=_is_captcha_like_text(
                error_text,
                status_code=None,
            ),
            session_age_hours=session_age_hours,
            total_requests_since_start=total_requests_since_start,
            history_length=history_length,
            input_length=input_length,
            search_type=search_type,
            output_instruction=output_instruction,
            prompt_style=prompt_style,
            preview=error_text[:200],
            error=error_text,
            started_at=started_at,
        )


def _trim_last(
    messages: list[dict[str, Any]], last_n: int | None
) -> list[dict[str, Any]]:
    if last_n is None or last_n <= 0 or last_n >= len(messages):
        return list(messages)
    return list(messages[-last_n:])


def _single_user_message(prompt: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": prompt}]


def _append_user_prompt(
    messages: list[dict[str, Any]], prompt: str
) -> list[dict[str, Any]]:
    return [*messages, {"role": "user", "content": prompt}]


def _build_history_messages(
    base_history: list[dict[str, Any]], history_length: int, prompt: str
) -> list[dict[str, Any]]:
    if history_length <= 0:
        return _single_user_message(prompt)
    return _append_user_prompt(_trim_last(base_history, history_length), prompt)


def _compose_query(base_query: str, output_instruction: str) -> str:
    return f"{base_query}\n\n{output_instruction}".strip()


def _parse_prompt_list(raw_value: str) -> list[str]:
    prompts = [item.strip() for item in raw_value.split("|") if item.strip()]
    return prompts or list(DEFAULT_CHAT_PROMPTS)


def _make_report_path(report_dir: Path, experiment_name: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return report_dir / f"{timestamp}-{experiment_name}.jsonl"


def _append_report_rows(report_path: Path, results: list[RequestResult]) -> None:
    if not results:
        return
    with report_path.open("a", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
        handle.flush()


def _group_summaries(results: list[RequestResult]) -> list[dict[str, Any]]:
    grouped: dict[str, list[RequestResult]] = {}
    for result in results:
        grouped.setdefault(result.group, []).append(result)

    summaries: list[dict[str, Any]] = []
    for group_name in sorted(grouped):
        group_results = grouped[group_name]
        durations = [result.response_time_s for result in group_results]
        summaries.append(
            {
                "group": group_name,
                "scenario": group_results[0].scenario,
                "count": len(group_results),
                "avg_response_time_s": round(sum(durations) / len(durations), 2),
                "max_response_time_s": max(durations),
                "timeout_count": sum(
                    1
                    for result in group_results
                    if "timed out" in (result.error or "").lower()
                ),
                "captcha_count": sum(
                    1 for result in group_results if result.captcha_detected
                ),
                "http_error_count": sum(
                    1 for result in group_results if result.status == "http_error"
                ),
                "history_length": group_results[0].history_length,
                "input_length": group_results[0].input_length,
                "search_type": group_results[0].search_type,
                "output_instruction": group_results[0].output_instruction,
                "prompt_style": group_results[0].prompt_style,
            }
        )
    return summaries


def _healthz_url_from_bridge_url(bridge_url: str) -> str:
    parsed = urllib.parse.urlsplit(bridge_url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/healthz", "", ""))


def _bridge_is_alive(bridge_url: str, timeout_seconds: float = 10.0) -> bool:
    request = urllib.request.Request(
        _healthz_url_from_bridge_url(bridge_url), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            parsed = json.loads(raw)
            return bool(parsed.get("ok")) and bool(parsed.get("ready"))
    except Exception:  # noqa: BLE001
        return False


def _validate_jsonl_report(report_path: Path) -> None:
    if not report_path.exists() or report_path.stat().st_size == 0:
        raise RuntimeError(f"Report file missing or empty: {report_path}")
    for line in report_path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def _run_smoke_checks(
    *,
    results: list[RequestResult],
    report_path: Path,
    bridge_url: str,
    consecutive_captcha_count: int,
    consecutive_captcha_limit: int,
    required_fields: set[str] | None = None,
    baseline_group: str | None = None,
    baseline_max_avg_response_s: float | None = None,
    baseline_allow_captcha: bool = True,
) -> None:
    _validate_jsonl_report(report_path)
    required_fields = required_fields or {
        "scenario",
        "response_time_s",
        "status_code",
        "captcha_detected",
    }
    for result in results:
        row = asdict(result)
        missing = required_fields - row.keys()
        if missing:
            raise RuntimeError(f"Missing required fields: {sorted(missing)}")

    if not any(
        summary["avg_response_time_s"] < 15 for summary in _group_summaries(results)
    ):
        raise RuntimeError(
            "Smoke check failed: every group is above 15 seconds, likely a global bridge problem."
        )

    if not _bridge_is_alive(bridge_url):
        raise RuntimeError(
            "Smoke check failed: Doubao bridge health check is not ready."
        )

    if baseline_group is not None and baseline_max_avg_response_s is not None:
        summaries = {summary["group"]: summary for summary in _group_summaries(results)}
        if baseline_group not in summaries:
            raise RuntimeError(
                f"Smoke check failed: baseline group {baseline_group} has no results yet."
            )
        baseline_summary = summaries[baseline_group]
        if baseline_summary["avg_response_time_s"] >= baseline_max_avg_response_s:
            raise RuntimeError(
                f"Smoke check failed: baseline group {baseline_group} average response time "
                f"{baseline_summary['avg_response_time_s']}s exceeds {baseline_max_avg_response_s}s."
            )
        if not baseline_allow_captcha and baseline_summary["captcha_count"] > 0:
            raise RuntimeError(
                f"Smoke check failed: baseline group {baseline_group} hit captcha unexpectedly."
            )

    if consecutive_captcha_count >= consecutive_captcha_limit:
        raise RuntimeError(
            f"Paused experiment after {consecutive_captcha_count} consecutive captcha-like results."
        )


def _print_summary(results: list[RequestResult], report_path: Path) -> None:
    captcha_results = [result for result in results if result.captcha_like]
    wait_values = [result.wait_s for result in results if result.wait_s is not None]
    avg_duration = (
        round(sum(result.duration_s for result in results) / len(results), 2)
        if results
        else 0.0
    )
    summary = {
        "report_path": str(report_path),
        "request_count": len(results),
        "captcha_like_count": len(captcha_results),
        "slow_30s_count": sum(1 for result in results if result.duration_s >= 30),
        "avg_duration_s": avg_duration,
        "max_duration_s": max((result.duration_s for result in results), default=0.0),
        "avg_wait_s": round(sum(wait_values) / len(wait_values), 2)
        if wait_values
        else None,
        "max_wait_s": max(wait_values, default=None),
        "first_captcha_label": captcha_results[0].label if captcha_results else None,
        "first_captcha_started_at": captcha_results[0].started_at
        if captcha_results
        else None,
    }
    print(json.dumps(summary, ensure_ascii=False))
    for group_summary in _group_summaries(results):
        print(json.dumps({"group_summary": group_summary}, ensure_ascii=False))
    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=False))


def run_matrix(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    db_path: Path,
    report_dir: Path,
    short_conversation_id: str,
    long_conversation_id: str,
    timeout_seconds: float,
    burst_repeats: int,
    burst_sleep_seconds: float,
    trim_values: list[int],
) -> Path:
    short_messages = _read_messages(db_path, short_conversation_id)
    long_messages = _read_messages(db_path, long_conversation_id)
    results: list[RequestResult] = []
    report_path = _make_report_path(report_dir, "matrix")

    for index in range(1, burst_repeats + 1):
        result = _post_chat_completion(
            bridge_url,
            short_messages,
            bridge_log_path=bridge_log_path,
            timeout_seconds=timeout_seconds,
            label=f"short_burst_{index}",
        )
        results.append(result)
        _append_report_rows(report_path, [result])
        if burst_sleep_seconds > 0:
            time.sleep(burst_sleep_seconds)

    for trim_value in trim_values:
        trimmed_messages = _trim_last(long_messages, trim_value)
        result = _post_chat_completion(
            bridge_url,
            trimmed_messages,
            bridge_log_path=bridge_log_path,
            timeout_seconds=timeout_seconds,
            label=f"long_trim_{trim_value}",
        )
        results.append(result)
        _append_report_rows(report_path, [result])
        if burst_sleep_seconds > 0:
            time.sleep(burst_sleep_seconds)

    alternating_pairs = min(3, len(trim_values))
    for index in range(alternating_pairs):
        trimmed_messages = _trim_last(long_messages, trim_values[index])
        long_result = _post_chat_completion(
            bridge_url,
            trimmed_messages,
            bridge_log_path=bridge_log_path,
            timeout_seconds=timeout_seconds,
            label=f"alt_long_{trim_values[index]}_{index + 1}",
        )
        short_result = _post_chat_completion(
            bridge_url,
            short_messages,
            bridge_log_path=bridge_log_path,
            timeout_seconds=timeout_seconds,
            label=f"alt_short_{index + 1}",
        )
        results.extend([long_result, short_result])
        _append_report_rows(report_path, [long_result, short_result])
        if burst_sleep_seconds > 0:
            time.sleep(burst_sleep_seconds)
    _print_summary(results, report_path)
    return report_path


def run_queue_stress(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    db_path: Path,
    report_dir: Path,
    short_conversation_id: str,
    long_conversation_id: str,
    timeout_seconds: float,
    max_workers: int,
    waves: int,
    inter_wave_sleep_seconds: float,
    stop_on_captcha: bool,
) -> Path:
    short_messages = _read_messages(db_path, short_conversation_id)
    long_messages = _read_messages(db_path, long_conversation_id)
    report_path = _make_report_path(report_dir, "queue-stress")
    stress_plan: list[tuple[str, list[dict[str, Any]]]] = [
        ("stress_long_176_a", _trim_last(long_messages, 176)),
        ("stress_short_a", short_messages),
        ("stress_long_120_a", _trim_last(long_messages, 120)),
        ("stress_short_b", short_messages),
        ("stress_long_176_b", _trim_last(long_messages, 176)),
        ("stress_short_c", short_messages),
    ]

    results: list[RequestResult] = []
    for wave_index in range(1, max(waves, 1) + 1):
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    _post_chat_completion,
                    bridge_url,
                    messages,
                    bridge_log_path=bridge_log_path,
                    timeout_seconds=timeout_seconds,
                    label=f"wave_{wave_index}_{label}",
                )
                for label, messages in stress_plan
            ]
            wave_results = [future.result() for future in futures]
            results.extend(wave_results)
            _append_report_rows(report_path, wave_results)

        if stop_on_captcha and any(result.captcha_like for result in wave_results):
            break
        if inter_wave_sleep_seconds > 0 and wave_index < max(waves, 1):
            time.sleep(inter_wave_sleep_seconds)

    _print_summary(results, report_path)
    return report_path


def run_session_decay(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    db_path: Path,
    report_dir: Path,
    short_conversation_id: str,
    long_conversation_id: str,
    timeout_seconds: float,
    waves: int,
    inter_wave_sleep_seconds: float,
    long_trim: int,
    short_repeats: int,
    stop_on_captcha: bool,
) -> Path:
    short_messages = _read_messages(db_path, short_conversation_id)
    long_messages = _trim_last(
        _read_messages(db_path, long_conversation_id),
        long_trim,
    )
    results: list[RequestResult] = []
    report_path = _make_report_path(report_dir, "session-decay")

    for wave_index in range(1, max(waves, 1) + 1):
        wave_results: list[RequestResult] = []
        for short_index in range(1, max(short_repeats, 1) + 1):
            wave_results.append(
                _post_chat_completion(
                    bridge_url,
                    short_messages,
                    bridge_log_path=bridge_log_path,
                    timeout_seconds=timeout_seconds,
                    label=f"wave_{wave_index}_short_{short_index}",
                )
            )
        wave_results.append(
            _post_chat_completion(
                bridge_url,
                long_messages,
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_long_{len(long_messages)}",
            )
        )
        results.extend(wave_results)
        _append_report_rows(report_path, wave_results)

        if stop_on_captcha and any(result.captcha_like for result in wave_results):
            break
        if inter_wave_sleep_seconds > 0 and wave_index < max(waves, 1):
            time.sleep(inter_wave_sleep_seconds)

    _print_summary(results, report_path)
    return report_path


def run_search_contrast(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    report_dir: Path,
    timeout_seconds: float,
    waves: int,
    inter_wave_sleep_seconds: float,
    chat_prompts: list[str],
    search_prompts: list[str],
    prompt_sleep_seconds: float,
    stop_on_captcha: bool,
) -> Path:
    report_path = _make_report_path(report_dir, "search-contrast")
    results: list[RequestResult] = []
    pair_count = min(len(chat_prompts), len(search_prompts))

    for wave_index in range(1, max(waves, 1) + 1):
        wave_results: list[RequestResult] = []
        for prompt_index in range(pair_count):
            chat_result = _post_chat_completion(
                bridge_url,
                _single_user_message(chat_prompts[prompt_index]),
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_chat_{prompt_index + 1}",
            )
            search_result = _post_chat_completion(
                bridge_url,
                _single_user_message(search_prompts[prompt_index]),
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_search_{prompt_index + 1}",
            )
            wave_results.extend([chat_result, search_result])
            _append_report_rows(report_path, [chat_result, search_result])
            if prompt_sleep_seconds > 0 and prompt_index < pair_count - 1:
                time.sleep(prompt_sleep_seconds)

        results.extend(wave_results)
        if stop_on_captcha and any(result.captcha_like for result in wave_results):
            break
        if inter_wave_sleep_seconds > 0 and wave_index < max(waves, 1):
            time.sleep(inter_wave_sleep_seconds)

    _print_summary(results, report_path)
    return report_path


def run_interaction_matrix(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    db_path: Path,
    report_dir: Path,
    long_conversation_id: str,
    timeout_seconds: float,
    waves: int,
    inter_wave_sleep_seconds: float,
    prompt_sleep_seconds: float,
    interaction_chat_prompt: str,
    interaction_search_prompt: str,
    interaction_long_trim: int,
    stop_on_captcha: bool,
) -> Path:
    long_history = _trim_last(
        _read_messages(db_path, long_conversation_id),
        interaction_long_trim,
    )
    case_plan: list[tuple[str, list[dict[str, Any]]]] = [
        ("short_chat", _single_user_message(interaction_chat_prompt)),
        ("short_search", _single_user_message(interaction_search_prompt)),
        ("long_chat", _append_user_prompt(long_history, interaction_chat_prompt)),
        ("long_search", _append_user_prompt(long_history, interaction_search_prompt)),
    ]
    report_path = _make_report_path(report_dir, "interaction-matrix")
    results: list[RequestResult] = []

    for wave_index in range(1, max(waves, 1) + 1):
        order_offset = (wave_index - 1) % len(case_plan)
        ordered_cases = case_plan[order_offset:] + case_plan[:order_offset]
        wave_results: list[RequestResult] = []
        for case_index, (case_label, messages) in enumerate(ordered_cases, start=1):
            result = _post_chat_completion(
                bridge_url,
                messages,
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_{case_label}",
            )
            wave_results.append(result)
            _append_report_rows(report_path, [result])
            if stop_on_captcha and result.captcha_like:
                break
            if prompt_sleep_seconds > 0 and case_index < len(ordered_cases):
                time.sleep(prompt_sleep_seconds)

        results.extend(wave_results)
        if stop_on_captcha and any(result.captcha_like for result in wave_results):
            break
        if inter_wave_sleep_seconds > 0 and wave_index < max(waves, 1):
            time.sleep(inter_wave_sleep_seconds)

    _print_summary(results, report_path)
    return report_path


def run_history_gradient(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    db_path: Path,
    report_dir: Path,
    long_conversation_id: str,
    timeout_seconds: float,
    waves: int,
    history_lengths: list[int],
    history_search_prompt: str,
    history_group_cooldown_seconds: float,
    history_random_seed: int,
    stop_on_captcha: bool,
    consecutive_captcha_limit: int,
) -> Path:
    base_history = _read_messages(db_path, long_conversation_id)
    report_path = _make_report_path(report_dir, "history-gradient")
    results: list[RequestResult] = []
    consecutive_captcha_count = 0

    for wave_index in range(1, max(waves, 1) + 1):
        ordered_lengths = list(history_lengths)
        random.Random(history_random_seed + wave_index).shuffle(ordered_lengths)
        wave_results: list[RequestResult] = []

        for length_index, history_length in enumerate(ordered_lengths, start=1):
            messages = _build_history_messages(
                base_history, history_length, history_search_prompt
            )
            result = _post_chat_completion(
                bridge_url,
                messages,
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_history_{history_length}",
                scenario="history-gradient",
                group=f"history_{history_length}",
                history_length=history_length,
                prompt_style="normal_search",
            )
            wave_results.append(result)
            _append_report_rows(report_path, [result])

            if result.captcha_detected:
                consecutive_captcha_count += 1
            else:
                consecutive_captcha_count = 0

            if stop_on_captcha and result.captcha_detected:
                break

            if history_group_cooldown_seconds > 0 and length_index < len(
                ordered_lengths
            ):
                time.sleep(history_group_cooldown_seconds)

        results.extend(wave_results)
        _run_smoke_checks(
            results=results,
            report_path=report_path,
            bridge_url=bridge_url,
            consecutive_captcha_count=consecutive_captcha_count,
            consecutive_captcha_limit=consecutive_captcha_limit,
        )

        if stop_on_captcha and any(result.captcha_detected for result in wave_results):
            break

    _print_summary(results, report_path)
    return report_path


def run_wording_contrast(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    db_path: Path,
    report_dir: Path,
    long_conversation_id: str,
    timeout_seconds: float,
    waves: int,
    wording_history_length: int,
    wording_group_cooldown_seconds: float,
    wording_random_seed: int,
    wording_normal_prompt: str,
    wording_complex_prompt: str,
    wording_roleplay_prompt: str,
    stop_on_captcha: bool,
    consecutive_captcha_limit: int,
) -> Path:
    base_history = _read_messages(db_path, long_conversation_id)
    prompts: list[tuple[str, str]] = [
        ("normal", wording_normal_prompt),
        ("complex", wording_complex_prompt),
        ("roleplay", wording_roleplay_prompt),
    ]
    report_path = _make_report_path(report_dir, "wording-contrast")
    results: list[RequestResult] = []
    consecutive_captcha_count = 0

    for wave_index in range(1, max(waves, 1) + 1):
        ordered_prompts = list(prompts)
        random.Random(wording_random_seed + wave_index).shuffle(ordered_prompts)
        wave_results: list[RequestResult] = []

        for prompt_index, (prompt_style, prompt) in enumerate(ordered_prompts, start=1):
            messages = _build_history_messages(
                base_history, wording_history_length, prompt
            )
            result = _post_chat_completion(
                bridge_url,
                messages,
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_{prompt_style}",
                scenario="wording-contrast",
                group=prompt_style,
                history_length=wording_history_length,
                prompt_style=prompt_style,
            )
            wave_results.append(result)
            _append_report_rows(report_path, [result])

            if result.captcha_detected:
                consecutive_captcha_count += 1
            else:
                consecutive_captcha_count = 0

            if stop_on_captcha and result.captcha_detected:
                break

            if wording_group_cooldown_seconds > 0 and prompt_index < len(
                ordered_prompts
            ):
                time.sleep(wording_group_cooldown_seconds)

        results.extend(wave_results)
        _run_smoke_checks(
            results=results,
            report_path=report_path,
            bridge_url=bridge_url,
            consecutive_captcha_count=consecutive_captcha_count,
            consecutive_captcha_limit=consecutive_captcha_limit,
        )

        if stop_on_captcha and any(result.captcha_detected for result in wave_results):
            break

    _print_summary(results, report_path)
    return report_path


def run_load_cube(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    db_path: Path,
    report_dir: Path,
    long_conversation_id: str,
    timeout_seconds: float,
    waves: int,
    load_long_input_length: int,
    load_chat_query: str,
    load_search_query: str,
    load_short_output_instruction: str,
    load_long_output_instruction: str,
    load_base_cooldown_seconds: float,
    load_search_cooldown_seconds: float,
    load_pause_on_captcha_seconds: float,
    load_pause_after_consecutive_captchas: int,
    load_random_seed: int,
    consecutive_captcha_limit: int,
) -> Path:
    base_history = _read_messages(db_path, long_conversation_id)
    report_path = _make_report_path(report_dir, "load-cube")
    results: list[RequestResult] = []
    consecutive_captcha_count = 0
    group_attempts: dict[str, int] = {}
    group_captchas: dict[str, int] = {}
    dangerous_groups: set[str] = set()

    cube_cases = [
        {
            "group": "G1",
            "input_length": 0,
            "search_type": "chat",
            "output_instruction": "short",
            "base_query": load_chat_query,
            "output_text": load_short_output_instruction,
        },
        {
            "group": "G2",
            "input_length": load_long_input_length,
            "search_type": "chat",
            "output_instruction": "short",
            "base_query": load_chat_query,
            "output_text": load_short_output_instruction,
        },
        {
            "group": "G3",
            "input_length": 0,
            "search_type": "chat",
            "output_instruction": "long",
            "base_query": load_chat_query,
            "output_text": load_long_output_instruction,
        },
        {
            "group": "G4",
            "input_length": 0,
            "search_type": "heavy_search",
            "output_instruction": "short",
            "base_query": load_search_query,
            "output_text": load_short_output_instruction,
        },
        {
            "group": "G5",
            "input_length": load_long_input_length,
            "search_type": "chat",
            "output_instruction": "long",
            "base_query": load_chat_query,
            "output_text": load_long_output_instruction,
        },
        {
            "group": "G6",
            "input_length": load_long_input_length,
            "search_type": "heavy_search",
            "output_instruction": "short",
            "base_query": load_search_query,
            "output_text": load_short_output_instruction,
        },
        {
            "group": "G7",
            "input_length": 0,
            "search_type": "heavy_search",
            "output_instruction": "long",
            "base_query": load_search_query,
            "output_text": load_long_output_instruction,
        },
        {
            "group": "G8",
            "input_length": load_long_input_length,
            "search_type": "heavy_search",
            "output_instruction": "long",
            "base_query": load_search_query,
            "output_text": load_long_output_instruction,
        },
    ]

    for wave_index in range(1, max(waves, 1) + 1):
        ordered_cases = [
            case for case in cube_cases if case["group"] not in dangerous_groups
        ]
        random.Random(load_random_seed + wave_index).shuffle(ordered_cases)
        wave_results: list[RequestResult] = []

        for case_index, case in enumerate(ordered_cases, start=1):
            prompt = _compose_query(case["base_query"], case["output_text"])
            messages = _build_history_messages(
                base_history, int(case["input_length"]), prompt
            )
            result = _post_chat_completion(
                bridge_url,
                messages,
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_{case['group']}",
                scenario="load-cube",
                group=str(case["group"]),
                history_length=int(case["input_length"]),
                input_length=int(case["input_length"]),
                search_type=str(case["search_type"]),
                output_instruction=str(case["output_instruction"]),
                prompt_style=f"{case['search_type']}_{case['output_instruction']}",
            )
            wave_results.append(result)
            _append_report_rows(report_path, [result])

            group_attempts[result.group] = group_attempts.get(result.group, 0) + 1
            group_captchas[result.group] = group_captchas.get(result.group, 0) + int(
                result.captcha_detected
            )
            if (
                group_attempts[result.group] >= max(waves, 1)
                and group_captchas[result.group] == group_attempts[result.group]
            ):
                dangerous_groups.add(result.group)

            if result.captcha_detected:
                consecutive_captcha_count += 1
                if consecutive_captcha_count >= consecutive_captcha_limit:
                    _run_smoke_checks(
                        results=results + wave_results,
                        report_path=report_path,
                        bridge_url=bridge_url,
                        consecutive_captcha_count=consecutive_captcha_count,
                        consecutive_captcha_limit=consecutive_captcha_limit,
                    )
                if consecutive_captcha_count >= load_pause_after_consecutive_captchas:
                    time.sleep(load_pause_on_captcha_seconds)
            else:
                consecutive_captcha_count = 0

            if case_index < len(ordered_cases):
                cooldown = (
                    load_search_cooldown_seconds
                    if case["search_type"] == "heavy_search"
                    else load_base_cooldown_seconds
                )
                if cooldown > 0:
                    time.sleep(cooldown)

        results.extend(wave_results)
        _run_smoke_checks(
            results=results,
            report_path=report_path,
            bridge_url=bridge_url,
            consecutive_captcha_count=consecutive_captcha_count,
            consecutive_captcha_limit=consecutive_captcha_limit,
            required_fields={
                "scenario",
                "input_length",
                "search_type",
                "output_instruction",
                "response_time_s",
                "status_code",
                "captcha_detected",
            },
            baseline_group="G1",
            baseline_max_avg_response_s=15.0,
            baseline_allow_captcha=False,
        )

    _print_summary(results, report_path)
    return report_path


def run_prompt_fingerprint(
    *,
    bridge_url: str,
    bridge_log_path: Path,
    report_dir: Path,
    timeout_seconds: float,
    prompt_file: Path | None,
    capture_jsonl: Path | None,
    capture_offset: int,
    waves: int,
    fingerprint_group_cooldown_seconds: float,
    fingerprint_random_seed: int,
    fingerprint_pause_on_captcha_seconds: float,
    fingerprint_pause_after_consecutive_captchas: int,
    consecutive_captcha_limit: int,
) -> Path:
    raw_prompt = _load_raw_prompt(
        prompt_file=prompt_file,
        capture_jsonl=capture_jsonl,
        capture_offset=capture_offset,
    )
    report_path = _make_report_path(report_dir, "prompt-fingerprint")
    variants = _build_prompt_fingerprint_variants(raw_prompt)
    _write_prompt_variant_samples(report_path=report_path, variants=variants)

    results: list[RequestResult] = []
    consecutive_captcha_count = 0

    for wave_index in range(1, max(waves, 1) + 1):
        ordered_variants = list(variants)
        random.Random(fingerprint_random_seed + wave_index).shuffle(ordered_variants)
        wave_results: list[RequestResult] = []

        for variant_index, (variant_name, prompt_text) in enumerate(
            ordered_variants, start=1
        ):
            result = _post_chat_completion(
                bridge_url,
                None,
                bridge_log_path=bridge_log_path,
                timeout_seconds=timeout_seconds,
                label=f"wave_{wave_index}_{variant_name}",
                raw_prompt=prompt_text,
                scenario="prompt-fingerprint",
                group=variant_name,
                prompt_style=variant_name,
            )
            wave_results.append(result)
            _append_report_rows(report_path, [result])

            if result.captcha_detected:
                consecutive_captcha_count += 1
                if (
                    consecutive_captcha_count
                    >= fingerprint_pause_after_consecutive_captchas
                ):
                    time.sleep(fingerprint_pause_on_captcha_seconds)
            else:
                consecutive_captcha_count = 0

            if fingerprint_group_cooldown_seconds > 0 and variant_index < len(
                ordered_variants
            ):
                time.sleep(fingerprint_group_cooldown_seconds)

        results.extend(wave_results)
        _run_smoke_checks(
            results=results,
            report_path=report_path,
            bridge_url=bridge_url,
            consecutive_captcha_count=consecutive_captcha_count,
            consecutive_captcha_limit=consecutive_captcha_limit,
            required_fields={
                "scenario",
                "group",
                "prompt_style",
                "response_time_s",
                "status_code",
                "captcha_detected",
            },
            baseline_group="B5_last_user_only",
            baseline_max_avg_response_s=15.0,
            baseline_allow_captcha=False,
        )

    _print_summary(results, report_path)
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay real AstrBot conversations against the Doubao web bridge."
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "matrix",
            "queue-stress",
            "session-decay",
            "search-contrast",
            "interaction-matrix",
            "history-gradient",
            "wording-contrast",
            "load-cube",
            "prompt-fingerprint",
        ),
        default="matrix",
    )
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--bridge-log-path", type=Path, default=DEFAULT_BRIDGE_LOG_PATH)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument(
        "--short-conversation-id", default=DEFAULT_SHORT_CONVERSATION_ID
    )
    parser.add_argument("--long-conversation-id", default=DEFAULT_LONG_CONVERSATION_ID)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--burst-repeats", type=int, default=4)
    parser.add_argument("--burst-sleep-seconds", type=float, default=2.0)
    parser.add_argument("--trim-values", default="20,60,120,176")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--waves", type=int, default=1)
    parser.add_argument("--inter-wave-sleep-seconds", type=float, default=3.0)
    parser.add_argument("--stop-on-captcha", action="store_true")
    parser.add_argument("--session-decay-long-trim", type=int, default=60)
    parser.add_argument("--session-decay-short-repeats", type=int, default=3)
    parser.add_argument("--chat-prompts", default="|".join(DEFAULT_CHAT_PROMPTS))
    parser.add_argument("--search-prompts", default="|".join(DEFAULT_SEARCH_PROMPTS))
    parser.add_argument("--prompt-sleep-seconds", type=float, default=2.0)
    parser.add_argument(
        "--interaction-chat-prompt", default=DEFAULT_INTERACTION_CHAT_PROMPT
    )
    parser.add_argument(
        "--interaction-search-prompt", default=DEFAULT_INTERACTION_SEARCH_PROMPT
    )
    parser.add_argument("--interaction-long-trim", type=int, default=120)
    parser.add_argument("--history-lengths", default="0,30,60,120,176")
    parser.add_argument(
        "--history-search-prompt", default=DEFAULT_HISTORY_GRADIENT_PROMPT
    )
    parser.add_argument("--history-group-cooldown-seconds", type=float, default=60.0)
    parser.add_argument("--history-random-seed", type=int, default=42)
    parser.add_argument("--consecutive-captcha-limit", type=int, default=3)
    parser.add_argument("--wording-history-length", type=int, default=120)
    parser.add_argument("--wording-group-cooldown-seconds", type=float, default=10.0)
    parser.add_argument("--wording-random-seed", type=int, default=42)
    parser.add_argument(
        "--wording-normal-prompt", default=DEFAULT_WORDING_NORMAL_PROMPT
    )
    parser.add_argument(
        "--wording-complex-prompt", default=DEFAULT_WORDING_COMPLEX_PROMPT
    )
    parser.add_argument(
        "--wording-roleplay-prompt", default=DEFAULT_WORDING_ROLEPLAY_PROMPT
    )
    parser.add_argument("--load-long-input-length", type=int, default=120)
    parser.add_argument("--load-chat-query", default=DEFAULT_LOAD_CUBE_CHAT_QUERY)
    parser.add_argument("--load-search-query", default=DEFAULT_LOAD_CUBE_SEARCH_QUERY)
    parser.add_argument(
        "--load-short-output-instruction", default=DEFAULT_LOAD_CUBE_SHORT_OUTPUT
    )
    parser.add_argument(
        "--load-long-output-instruction", default=DEFAULT_LOAD_CUBE_LONG_OUTPUT
    )
    parser.add_argument("--load-base-cooldown-seconds", type=float, default=90.0)
    parser.add_argument("--load-search-cooldown-seconds", type=float, default=120.0)
    parser.add_argument("--load-pause-on-captcha-seconds", type=float, default=600.0)
    parser.add_argument("--load-pause-after-consecutive-captchas", type=int, default=2)
    parser.add_argument("--load-random-seed", type=int, default=42)
    parser.add_argument(
        "--fingerprint-prompt-file", type=Path, default=DEFAULT_PROMPT_SAMPLE_PATH
    )
    parser.add_argument("--fingerprint-capture-jsonl", type=Path, default=None)
    parser.add_argument("--fingerprint-capture-offset", type=int, default=1)
    parser.add_argument(
        "--fingerprint-group-cooldown-seconds", type=float, default=60.0
    )
    parser.add_argument("--fingerprint-random-seed", type=int, default=42)
    parser.add_argument(
        "--fingerprint-pause-on-captcha-seconds", type=float, default=600.0
    )
    parser.add_argument(
        "--fingerprint-pause-after-consecutive-captchas", type=int, default=2
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    trim_values = [int(value) for value in args.trim_values.split(",") if value.strip()]
    history_lengths = [
        int(value) for value in args.history_lengths.split(",") if value.strip()
    ]
    chat_prompts = _parse_prompt_list(args.chat_prompts)
    search_prompts = _parse_prompt_list(args.search_prompts)
    if args.scenario == "matrix":
        run_matrix(
            bridge_url=args.bridge_url,
            bridge_log_path=args.bridge_log_path,
            db_path=args.db_path,
            report_dir=args.report_dir,
            short_conversation_id=args.short_conversation_id,
            long_conversation_id=args.long_conversation_id,
            timeout_seconds=args.timeout_seconds,
            burst_repeats=max(args.burst_repeats, 1),
            burst_sleep_seconds=max(args.burst_sleep_seconds, 0.0),
            trim_values=trim_values or [20, 60, 120, 176],
        )
    elif args.scenario == "queue-stress":
        run_queue_stress(
            bridge_url=args.bridge_url,
            bridge_log_path=args.bridge_log_path,
            db_path=args.db_path,
            report_dir=args.report_dir,
            short_conversation_id=args.short_conversation_id,
            long_conversation_id=args.long_conversation_id,
            timeout_seconds=args.timeout_seconds,
            max_workers=max(args.max_workers, 1),
            waves=max(args.waves, 1),
            inter_wave_sleep_seconds=max(args.inter_wave_sleep_seconds, 0.0),
            stop_on_captcha=args.stop_on_captcha,
        )
    else:
        if args.scenario == "search-contrast":
            run_search_contrast(
                bridge_url=args.bridge_url,
                bridge_log_path=args.bridge_log_path,
                report_dir=args.report_dir,
                timeout_seconds=args.timeout_seconds,
                waves=max(args.waves, 1),
                inter_wave_sleep_seconds=max(args.inter_wave_sleep_seconds, 0.0),
                chat_prompts=chat_prompts,
                search_prompts=search_prompts,
                prompt_sleep_seconds=max(args.prompt_sleep_seconds, 0.0),
                stop_on_captcha=args.stop_on_captcha,
            )
            return 0
        if args.scenario == "interaction-matrix":
            run_interaction_matrix(
                bridge_url=args.bridge_url,
                bridge_log_path=args.bridge_log_path,
                db_path=args.db_path,
                report_dir=args.report_dir,
                long_conversation_id=args.long_conversation_id,
                timeout_seconds=args.timeout_seconds,
                waves=max(args.waves, 1),
                inter_wave_sleep_seconds=max(args.inter_wave_sleep_seconds, 0.0),
                prompt_sleep_seconds=max(args.prompt_sleep_seconds, 0.0),
                interaction_chat_prompt=args.interaction_chat_prompt,
                interaction_search_prompt=args.interaction_search_prompt,
                interaction_long_trim=max(args.interaction_long_trim, 1),
                stop_on_captcha=args.stop_on_captcha,
            )
            return 0
        if args.scenario == "history-gradient":
            run_history_gradient(
                bridge_url=args.bridge_url,
                bridge_log_path=args.bridge_log_path,
                db_path=args.db_path,
                report_dir=args.report_dir,
                long_conversation_id=args.long_conversation_id,
                timeout_seconds=args.timeout_seconds,
                waves=max(args.waves, 1),
                history_lengths=history_lengths or [0, 30, 60, 120, 176],
                history_search_prompt=args.history_search_prompt,
                history_group_cooldown_seconds=max(
                    args.history_group_cooldown_seconds, 0.0
                ),
                history_random_seed=args.history_random_seed,
                stop_on_captcha=args.stop_on_captcha,
                consecutive_captcha_limit=max(args.consecutive_captcha_limit, 1),
            )
            return 0
        if args.scenario == "wording-contrast":
            run_wording_contrast(
                bridge_url=args.bridge_url,
                bridge_log_path=args.bridge_log_path,
                db_path=args.db_path,
                report_dir=args.report_dir,
                long_conversation_id=args.long_conversation_id,
                timeout_seconds=args.timeout_seconds,
                waves=max(args.waves, 1),
                wording_history_length=max(args.wording_history_length, 0),
                wording_group_cooldown_seconds=max(
                    args.wording_group_cooldown_seconds, 0.0
                ),
                wording_random_seed=args.wording_random_seed,
                wording_normal_prompt=args.wording_normal_prompt,
                wording_complex_prompt=args.wording_complex_prompt,
                wording_roleplay_prompt=args.wording_roleplay_prompt,
                stop_on_captcha=args.stop_on_captcha,
                consecutive_captcha_limit=max(args.consecutive_captcha_limit, 1),
            )
            return 0
        if args.scenario == "load-cube":
            run_load_cube(
                bridge_url=args.bridge_url,
                bridge_log_path=args.bridge_log_path,
                db_path=args.db_path,
                report_dir=args.report_dir,
                long_conversation_id=args.long_conversation_id,
                timeout_seconds=args.timeout_seconds,
                waves=max(args.waves, 1),
                load_long_input_length=max(args.load_long_input_length, 0),
                load_chat_query=args.load_chat_query,
                load_search_query=args.load_search_query,
                load_short_output_instruction=args.load_short_output_instruction,
                load_long_output_instruction=args.load_long_output_instruction,
                load_base_cooldown_seconds=max(args.load_base_cooldown_seconds, 0.0),
                load_search_cooldown_seconds=max(
                    args.load_search_cooldown_seconds, 0.0
                ),
                load_pause_on_captcha_seconds=max(
                    args.load_pause_on_captcha_seconds, 0.0
                ),
                load_pause_after_consecutive_captchas=max(
                    args.load_pause_after_consecutive_captchas, 1
                ),
                load_random_seed=args.load_random_seed,
                consecutive_captcha_limit=max(args.consecutive_captcha_limit, 1),
            )
            return 0
        if args.scenario == "prompt-fingerprint":
            run_prompt_fingerprint(
                bridge_url=args.bridge_url,
                bridge_log_path=args.bridge_log_path,
                report_dir=args.report_dir,
                timeout_seconds=args.timeout_seconds,
                prompt_file=args.fingerprint_prompt_file,
                capture_jsonl=args.fingerprint_capture_jsonl,
                capture_offset=max(args.fingerprint_capture_offset, 1),
                waves=max(args.waves, 1),
                fingerprint_group_cooldown_seconds=max(
                    args.fingerprint_group_cooldown_seconds,
                    0.0,
                ),
                fingerprint_random_seed=args.fingerprint_random_seed,
                fingerprint_pause_on_captcha_seconds=max(
                    args.fingerprint_pause_on_captcha_seconds,
                    0.0,
                ),
                fingerprint_pause_after_consecutive_captchas=max(
                    args.fingerprint_pause_after_consecutive_captchas,
                    1,
                ),
                consecutive_captcha_limit=max(args.consecutive_captcha_limit, 1),
            )
            return 0
        run_session_decay(
            bridge_url=args.bridge_url,
            bridge_log_path=args.bridge_log_path,
            db_path=args.db_path,
            report_dir=args.report_dir,
            short_conversation_id=args.short_conversation_id,
            long_conversation_id=args.long_conversation_id,
            timeout_seconds=args.timeout_seconds,
            waves=max(args.waves, 1),
            inter_wave_sleep_seconds=max(args.inter_wave_sleep_seconds, 0.0),
            long_trim=max(args.session_decay_long_trim, 1),
            short_repeats=max(args.session_decay_short_repeats, 1),
            stop_on_captcha=args.stop_on_captcha,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
