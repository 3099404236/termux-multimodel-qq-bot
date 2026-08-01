from types import SimpleNamespace

import pytest

from astrbot.core.message.components import Plain
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata


class ConcreteAstrMessageEvent(AstrMessageEvent):
    async def send(self, message):  # noqa: ANN001
        return None


class DummyPersonaManager:
    async def get_default_persona_v3(self, umo=None):  # noqa: ANN001, ARG002
        return {
            "name": "正能量2",
            "prompt": "你是一个聪明、正直、有幽默感的AI。你叫穿制服。是一个普通人。",
            "begin_dialogs": [],
            "mood_imitation_dialogs": [],
            "tools": None,
            "skills": None,
            "custom_error_message": None,
            "_begin_dialogs_processed": [],
            "_mood_imitation_dialogs_processed": "",
        }


def _make_stage_context():
    plugin_context = SimpleNamespace(persona_manager=DummyPersonaManager())
    plugin_manager = SimpleNamespace(context=plugin_context)
    return SimpleNamespace(
        astrbot_config={
            "platform_settings": {
                "no_permission_reply": False,
                "friend_message_needs_wake_prefix": False,
                "ignore_bot_self_message": False,
                "ignore_at_all": False,
                "unique_session": False,
            },
            "disable_builtin_commands": False,
            "admins_id": [],
            "wake_prefix": ["/"],
            "plugin_set": ["*"],
        },
        plugin_manager=plugin_manager,
        astrbot_config_id="default",
    )


def _make_event(message_str: str) -> ConcreteAstrMessageEvent:
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "bot-self-id"
    message.session_id = "602142028"
    message.group_id = "602142028"
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="3099404236", nickname="曾tea")
    message.message = [Plain(text=message_str)]
    message.message_str = message_str
    message.raw_message = None

    return ConcreteAstrMessageEvent(
        message_str=message_str,
        message_obj=message,
        platform_meta=PlatformMetadata(
            name="aiocqhttp",
            description="test",
            id="aiocqhttp",
        ),
        session_id="602142028",
    )


@pytest.mark.asyncio
async def test_plain_text_bot_name_mention_wakes_group_message(monkeypatch):
    async def _pass_handlers(event, handlers):  # noqa: ANN001
        return handlers

    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
        staticmethod(_pass_handlers),
    )

    stage = WakingCheckStage()
    await stage.initialize(_make_stage_context())
    event = _make_event("@穿制服 evolpromopt是什么")

    await stage.process(event)

    assert event.is_wake is True
    assert event.is_at_or_wake_command is True
    assert event.is_stopped() is False
    assert event.message_str == "evolpromopt是什么"


@pytest.mark.asyncio
async def test_plain_text_other_name_mention_does_not_wake(monkeypatch):
    async def _pass_handlers(event, handlers):  # noqa: ANN001
        return handlers

    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
        staticmethod(_pass_handlers),
    )

    stage = WakingCheckStage()
    await stage.initialize(_make_stage_context())
    event = _make_event("@别人 evolpromopt是什么")

    await stage.process(event)

    assert event.is_wake is False
    assert event.is_at_or_wake_command is False
    assert event.is_stopped() is True
