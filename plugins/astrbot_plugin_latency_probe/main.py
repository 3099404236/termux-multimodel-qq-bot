from __future__ import annotations

import json
import time
from sys import maxsize
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

STATE_KEY = "_latency_probe_state"


@register(
    "astrbot_plugin_latency_probe",
    "Codex",
    "Record queue, model, decoration, sending, and total reply latency.",
    "1.0.0",
)
class LatencyProbePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @staticmethod
    def _state(event: AstrMessageEvent) -> dict[str, Any]:
        state = event.get_extra(STATE_KEY)
        if not isinstance(state, dict):
            state = {
                "received_at": time.monotonic(),
                "span_id": event.trace.span_id,
            }
            event.set_extra(STATE_KEY, state)
        return state

    @staticmethod
    def _seconds(end: float | None, start: float | None) -> float | None:
        if end is None or start is None:
            return None
        return round(max(end - start, 0.0), 3)

    @staticmethod
    def _emit(phase: str, event: AstrMessageEvent, **fields: Any) -> None:
        state = LatencyProbePlugin._state(event)
        payload = {
            "phase": phase,
            "span_id": state["span_id"],
            "umo": event.unified_msg_origin,
            "sender_id": event.get_sender_id(),
            **fields,
        }
        logger.info("[Latency] %s", json.dumps(payload, ensure_ascii=False))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize - 10)
    async def on_message(self, event: AstrMessageEvent) -> None:
        self._state(event)

    @filter.on_waiting_llm_request(priority=maxsize - 10)
    async def on_waiting_llm_request(self, event: AstrMessageEvent) -> None:
        state = self._state(event)
        state.setdefault("queue_entered_at", time.monotonic())

    @filter.on_llm_request(priority=maxsize - 10)
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        request: ProviderRequest,
    ) -> None:
        del request
        state = self._state(event)
        state.setdefault("llm_started_at", time.monotonic())
        self._emit(
            "llm_start",
            event,
            intake_s=self._seconds(
                state.get("queue_entered_at"),
                state.get("received_at"),
            ),
            session_queue_s=self._seconds(
                state.get("llm_started_at"),
                state.get("queue_entered_at"),
            ),
        )

    @filter.on_llm_response(priority=-maxsize)
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        state = self._state(event)
        state["llm_finished_at"] = time.monotonic()
        state["response_id"] = response.id
        self._emit(
            "llm_done",
            event,
            model_and_bridge_s=self._seconds(
                state.get("llm_finished_at"),
                state.get("llm_started_at"),
            ),
            response_id=response.id,
        )

    @filter.on_decorating_result(priority=-maxsize)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        state = self._state(event)
        state["decorated_at"] = time.monotonic()

    @filter.after_message_sent(priority=-maxsize)
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        state = self._state(event)
        sent_at = time.monotonic()
        self._emit(
            "sent",
            event,
            intake_s=self._seconds(
                state.get("queue_entered_at"),
                state.get("received_at"),
            ),
            session_queue_s=self._seconds(
                state.get("llm_started_at"),
                state.get("queue_entered_at"),
            ),
            model_and_bridge_s=self._seconds(
                state.get("llm_finished_at"),
                state.get("llm_started_at"),
            ),
            decorate_s=self._seconds(
                state.get("decorated_at"),
                state.get("llm_finished_at"),
            ),
            qq_send_s=self._seconds(sent_at, state.get("decorated_at")),
            total_s=self._seconds(sent_at, state.get("received_at")),
            response_id=state.get("response_id"),
        )
