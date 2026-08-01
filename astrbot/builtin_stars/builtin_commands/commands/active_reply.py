from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult


class ActiveReplyCommands:
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def active_reply(self, event: AstrMessageEvent, action: str = "") -> None:
        if not event.is_private_chat():
            self._finish(event, "这个命令只允许私聊使用。")
            return

        cfg = self.context.get_config()
        active_reply_cfg = cfg["provider_ltm_settings"]["active_reply"]
        enabled = bool(active_reply_cfg.get("enable", False))
        method = active_reply_cfg.get("method", "unknown")
        normalized = action.strip().lower()

        if not normalized or normalized in {"status", "状态", "查询", "查看"}:
            status_text = "开启" if enabled else "关闭"
            self._finish(
                event,
                f"当前默认配置的群聊跟进回复：{status_text}\n触发方式：{method}\n用法：/stack 主动回复 状态|开启|关闭",
            )
            return

        if normalized in {"on", "开启", "打开", "开", "启用"}:
            if enabled:
                self._finish(event, "默认配置的群聊跟进回复已经是开启状态。")
                return
            active_reply_cfg["enable"] = True
            cfg.save_config()
            self._finish(event, "已开启默认配置的群聊跟进回复。")
            return

        if normalized in {"off", "关闭", "关", "停用", "禁用"}:
            if not enabled:
                self._finish(event, "默认配置的群聊跟进回复已经是关闭状态。")
                return
            active_reply_cfg["enable"] = False
            cfg.save_config()
            self._finish(event, "已关闭默认配置的群聊跟进回复。")
            return

        self._finish(event, "不支持这个动作。可用动作：状态 / 开启 / 关闭")

    def _finish(self, event: AstrMessageEvent, text: str) -> None:
        event.should_call_llm(False)
        event.set_result(MessageEventResult().message(text))
        event.stop_event()
