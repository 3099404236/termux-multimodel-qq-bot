from typing import Any

from astrbot import logger
from astrbot.core.platform.message_type import MessageType

_TIMEOUT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "readtimeout",
    "apitimeouterror",
    "超时",
)
_TIMEOUT_USER_MESSAGE = "本次生成超时，请稍后重试。"


def runtime_error_message_for_user(event: Any, error_text: str) -> str | None:
    """Return a safe user-facing error, or None when it should stay internal."""
    if event is None:
        return error_text

    try:
        is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
    except Exception:
        return error_text

    if not is_group:
        return error_text
    normalized = error_text.casefold()
    if any(marker in normalized for marker in _TIMEOUT_ERROR_MARKERS):
        return _TIMEOUT_USER_MESSAGE
    return None


def should_surface_runtime_error_to_user(
    event: Any,
    error_text: str | None = None,
) -> bool:
    """Return whether runtime/backend errors should be sent to the user."""
    if error_text is not None:
        return runtime_error_message_for_user(event, error_text) is not None
    return runtime_error_message_for_user(event, "") is not None


def log_suppressed_runtime_error(
    event: Any,
    *,
    source: str,
    error_text: str,
) -> None:
    umo = "unknown"
    sender_id = "unknown"
    try:
        umo = getattr(event, "unified_msg_origin", "unknown")
        sender_id = event.get_sender_id()
    except Exception:
        pass

    logger.warning(
        "Suppressed runtime error reply. source=%s umo=%s sender_id=%s error=%s",
        source,
        umo,
        sender_id,
        error_text,
    )
