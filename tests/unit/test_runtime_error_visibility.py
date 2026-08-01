from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.third_party import (
    _RunnerResultAggregator,
)
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.runtime_error_visibility import should_surface_runtime_error_to_user


class ConcreteAstrMessageEvent(AstrMessageEvent):
    async def send(self, message):  # noqa: ANN001
        return None


def _make_event(message_type: MessageType) -> ConcreteAstrMessageEvent:
    message = AstrBotMessage()
    message.type = message_type
    message.self_id = "bot-123"
    message.session_id = "group-1" if message_type == MessageType.GROUP_MESSAGE else "user-1"
    message.group_id = "group-1" if message_type == MessageType.GROUP_MESSAGE else ""
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="user-1", nickname="User One")
    message.message = [Plain(text="hello")]
    message.message_str = "hello"
    message.raw_message = None
    return ConcreteAstrMessageEvent(
        message_str="hello",
        message_obj=message,
        platform_meta=PlatformMetadata(
            name="test_platform",
            description="Test platform",
            id="test_platform_id",
        ),
        session_id=message.session_id,
    )


def test_group_runtime_errors_are_suppressed():
    event = _make_event(MessageType.GROUP_MESSAGE)

    assert should_surface_runtime_error_to_user(event) is False


def test_private_runtime_errors_are_visible():
    event = _make_event(MessageType.FRIEND_MESSAGE)

    assert should_surface_runtime_error_to_user(event) is True


def test_third_party_aggregator_returns_empty_chain_when_suppressed():
    aggregator = _RunnerResultAggregator()
    aggregator.add_chunk(MessageChain(), True)

    final_chain, is_error = aggregator.finalize(
        LLMResponse(role="err", completion_text="boom"),
        suppress_error_output=True,
    )

    assert final_chain == []
    assert is_error is True
