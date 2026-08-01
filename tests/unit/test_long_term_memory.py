from unittest.mock import MagicMock

import pytest

from astrbot.builtin_stars.astrbot.long_term_memory import (
    ACTIVE_REPLY_NO_REPLY_TOKEN,
    LongTermMemory,
)
from astrbot.core.message.components import At, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import LLMResponse, ProviderRequest


class ConcreteAstrMessageEvent(AstrMessageEvent):
    async def send(self, message):  # noqa: ANN001
        return None


class DummyContext:
    def __init__(self, config: dict) -> None:
        self._config = config

    def get_config(self, umo=None):  # noqa: ANN001, ARG002
        return self._config

    def get_using_provider(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    def get_provider_by_id(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _make_config() -> dict:
    return {
        "provider_settings": {
            "image_caption_prompt": "Describe the image.",
        },
        "provider_ltm_settings": {
            "group_icl_enable": True,
            "group_message_max_cnt": 20,
            "image_caption": False,
            "image_caption_provider_id": "",
            "active_reply": {
                "enable": True,
                "method": "rule_based_reply",
                "possibility_reply": 0.1,
                "prompt": "",
                "whitelist": [],
            },
        },
    }


def _make_event(
    *,
    message_str: str,
    sender_id: str = "user-1",
    sender_name: str = "User One",
    self_id: str = "bot-123",
    session_id: str = "group-1",
):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = self_id
    message.session_id = session_id
    message.group_id = session_id
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id=sender_id, nickname=sender_name)
    message.message = [Plain(text=message_str)]
    message.message_str = message_str
    message.raw_message = None

    return ConcreteAstrMessageEvent(
        message_str=message_str,
        message_obj=message,
        platform_meta=PlatformMetadata(
            name="test_platform",
            description="Test platform",
            id="test_platform_id",
        ),
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_rule_based_reply_triggers_on_bot_reference():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="机器人你怎么看这个问题")

    assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_rule_based_reply_skips_generic_question_without_follow_up_window():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="这个接口怎么写")

    assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_rule_based_reply_skips_image_only_message_without_anchor():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="[图片]")
    event.message_obj.message = [Image(file="https://example.com/test.png")]

    assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_rule_based_reply_skips_command_like_follow_up_message():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="/sid")
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = str(event.get_sender_id())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 10,
        )
        assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_rule_based_reply_skips_napcat_status_message():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="NapCat 信息")
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = str(event.get_sender_id())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 10,
        )
        assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_handle_message_skips_recording_command_like_group_message():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="##菜单")

    await ltm.handle_message(event)

    assert event.unified_msg_origin not in ltm.session_chats


@pytest.mark.asyncio
async def test_rule_based_reply_triggers_after_direct_invocation_follow_up_window():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    direct_event = _make_event(message_str="@bot 这个怎么处理")
    direct_event.message_obj.message = [
        At(qq=direct_event.get_self_id(), name="bot"),
        Plain(text="这个怎么处理"),
    ]
    direct_event.message_obj.message_str = "@bot 这个怎么处理"
    llm_resp = LLMResponse(role="assistant", completion_text="先这样试试")
    await ltm.handle_message(direct_event)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9,
        )
        await ltm.after_req_llm(direct_event, llm_resp)

    event = _make_event(message_str="那这个怎么处理")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 10,
        )
        assert await ltm.need_active_reply(event) is True


@pytest.mark.asyncio
async def test_rule_based_reply_triggers_same_sender_follow_up_detail_after_recent_bot_reply():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="我第一次打开啊")
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = str(event.get_sender_id())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 30,
        )
        assert await ltm.need_active_reply(event) is True


@pytest.mark.asyncio
async def test_rule_based_reply_triggers_same_sender_technical_follow_up_after_recent_bot_reply():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="GPU渲染加速off和on都没用")
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = str(event.get_sender_id())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 30,
        )
        assert await ltm.need_active_reply(event) is True


@pytest.mark.asyncio
async def test_rule_based_reply_bypasses_cooldown_for_same_sender_question_follow_up():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="什么是串号？")
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = str(event.get_sender_id())
    ltm.last_active_reply_at[event.unified_msg_origin] = 1e9

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 5,
        )
        assert await ltm.need_active_reply(event) is True


@pytest.mark.asyncio
async def test_rule_based_reply_skips_other_sender_follow_up_without_direct_intent():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="GPU渲染加速off和on都没用", sender_id="user-2")
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = "user-1"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 30,
        )
        assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_rule_based_reply_respects_cooldown():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="机器人帮我看下")
    ltm.last_active_reply_at[event.unified_msg_origin] = 1e9
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = str(event.get_sender_id())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 5,
        )
        assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_rule_based_reply_skips_follow_up_when_feature_disabled():
    config = _make_config()
    config["provider_ltm_settings"]["active_reply"]["enable"] = False
    ltm = LongTermMemory(MagicMock(), DummyContext(config))
    event = _make_event(message_str="什么是串号？")
    ltm.last_bot_reply_at[event.unified_msg_origin] = 1e9
    ltm.follow_up_sender_id[event.unified_msg_origin] = str(event.get_sender_id())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "astrbot.builtin_stars.astrbot.long_term_memory.time.monotonic",
            lambda: 1e9 + 5,
        )
        assert await ltm.need_active_reply(event) is False


@pytest.mark.asyncio
async def test_active_reply_prompt_excludes_current_recorded_message_from_history():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    previous_event = _make_event(
        message_str="上一条群消息",
        sender_id="user-2",
        sender_name="User Two",
    )
    current_event = _make_event(message_str="机器人你怎么看这个问题")

    await ltm.handle_message(previous_event)
    await ltm.handle_message(current_event)

    req = ProviderRequest()
    req.prompt = current_event.message_str
    req.contexts = []
    req.system_prompt = ""

    await ltm.on_req_llm(current_event, req)

    assert "上一条群消息" in req.prompt
    assert req.prompt.count("机器人你怎么看这个问题") == 1


@pytest.mark.asyncio
async def test_active_reply_prompt_adds_literal_response_guidance():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    current_event = _make_event(message_str="怎么又跌")
    current_event.set_extra("_active_reply_request", True)

    await ltm.handle_message(current_event)

    req = ProviderRequest()
    req.prompt = current_event.message_str
    req.contexts = []
    req.system_prompt = ""

    await ltm.on_req_llm(current_event, req)

    assert "Do not over-interpret short reactive remarks" in req.prompt
    assert "'又跌了', '又涨了', '又卡了', or '又崩了'" in req.prompt
    assert "Do not judge a price as cheap, expensive, fair, or outrageous" in req.prompt
    assert "If the basis is unclear, ask for the missing context or say you are not sure." in req.prompt
    assert ACTIVE_REPLY_NO_REPLY_TOKEN in req.prompt


@pytest.mark.asyncio
async def test_active_reply_suppresses_no_reply_token():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="136 caf怎么1400加了")
    event.set_extra("_active_reply_request", True)
    llm_resp = LLMResponse(
        role="assistant",
        completion_text=ACTIVE_REPLY_NO_REPLY_TOKEN,
    )

    suppressed = ltm.suppress_active_reply_response_if_needed(event, llm_resp)

    assert suppressed is True
    assert llm_resp.completion_text == ""
    assert llm_resp.result_chain is None


@pytest.mark.asyncio
async def test_active_reply_suppresses_no_reply_variants():
    ltm = LongTermMemory(MagicMock(), DummyContext(_make_config()))
    event = _make_event(message_str="这价有点离谱吗")
    event.set_extra("_active_reply_request", True)

    bare_variant = LLMResponse(role="assistant", completion_text="ASTRBOT_NO_REPLY")
    assert ltm.suppress_active_reply_response_if_needed(event, bare_variant) is True
    assert bare_variant.completion_text == ""

    natural_variant = LLMResponse(role="assistant", completion_text="no reply")
    assert ltm.suppress_active_reply_response_if_needed(event, natural_variant) is True
    assert natural_variant.completion_text == ""

    chain_variant = LLMResponse(
        role="assistant",
        result_chain=MessageChain().message("```__ASTRBOT_NO_REPLY__```"),
    )
    assert ltm.suppress_active_reply_response_if_needed(event, chain_variant) is True
    assert chain_variant.completion_text == ""
