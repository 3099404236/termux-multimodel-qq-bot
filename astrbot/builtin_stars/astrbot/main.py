import traceback

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core import logger

from .long_term_memory import LongTermMemory


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.ltm = None
        try:
            self.ltm = LongTermMemory(self.context.astrbot_config_mgr, self.context)
        except BaseException as e:
            logger.error(f"聊天增强 err: {e}")

    def ltm_enabled(self, event: AstrMessageEvent):
        ltmse = self.context.get_config(umo=event.unified_msg_origin)[
            "provider_ltm_settings"
        ]
        return ltmse["group_icl_enable"] or ltmse["active_reply"]["enable"]

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """群聊记忆增强"""
        has_image_or_plain = False
        for comp in event.message_obj.message:
            if isinstance(comp, Plain) or isinstance(comp, Image):
                has_image_or_plain = True
                break

        if self.ltm_enabled(event) and self.ltm and has_image_or_plain:
            ltm_settings = self.context.get_config(umo=event.unified_msg_origin)[
                "provider_ltm_settings"
            ]
            need_active = await self.ltm.need_active_reply(event)
            should_record_message = (
                ltm_settings["group_icl_enable"]
                or ltm_settings["active_reply"]["enable"]
            )
            if should_record_message:
                """记录对话"""
                try:
                    await self.ltm.handle_message(event)
                except BaseException as e:
                    logger.error(e)

            if need_active:
                """主动回复"""
                provider = self.context.get_using_provider(event.unified_msg_origin)
                if not provider:
                    logger.error("未找到任何 LLM 提供商。请先配置。无法主动回复")
                    return
                try:
                    conv = None
                    session_curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                        event.unified_msg_origin,
                    )

                    if not session_curr_cid:
                        logger.error(
                            "当前未处于对话状态，无法主动回复，请确保 平台设置->会话隔离(unique_session) 未开启，并使用 /switch 序号 切换或者 /new 创建一个会话。",
                        )
                        return

                    conv = await self.context.conversation_manager.get_conversation(
                        event.unified_msg_origin,
                        session_curr_cid,
                    )

                    prompt = event.message_str

                    if not conv:
                        logger.error("未找到对话，无法主动回复")
                        return

                    self.ltm.mark_active_reply_request(event)
                    yield event.request_llm(
                        prompt=prompt,
                        session_id=event.session_id,
                        conversation=conv,
                    )
                except BaseException as e:
                    logger.error(traceback.format_exc())
                    logger.error(f"主动回复失败: {e}")

    @filter.on_llm_request()
    async def decorate_llm_req(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在请求 LLM 前注入人格信息、Identifier、时间、回复内容等 System Prompt"""
        if self.ltm and self.ltm_enabled(event):
            try:
                await self.ltm.on_req_llm(event, req)
            except BaseException as e:
                logger.error(f"ltm: {e}")

    @filter.on_llm_response()
    async def record_llm_resp_to_ltm(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        """在 LLM 响应后记录对话"""
        if self.ltm and self.ltm_enabled(event):
            try:
                self.ltm.suppress_active_reply_response_if_needed(event, resp)
                await self.ltm.after_req_llm(event, resp)
            except Exception as e:
                logger.error(f"ltm: {e}")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后处理"""
        if self.ltm and self.ltm_enabled(event):
            try:
                clean_session = event.get_extra("_clean_ltm_session", False)
                if clean_session:
                    await self.ltm.remove_session(event)
            except Exception as e:
                logger.error(f"ltm: {e}")

    @filter.llm_tool(name="search_group_history")
    async def search_group_history(
        self,
        event: AstrMessageEvent,
        query: str = "",
        limit: int = 8,
        hours: int = 0,
    ) -> str:
        """搜索当前 QQ 群的历史原文和结构化摘要。当问题依赖更早的群聊、人物说法、决定、链接或时间线，而当前上下文不足时使用。只搜索当前群，不能跨群读取。

        Args:
            query(string): 搜索关键词、人物名、项目名或短语；留空时返回最近记忆。
            limit(number): 最多返回多少条原始消息，默认 8，最大 12。
            hours(number): 只搜索最近多少小时；0 表示不限时间。
        """
        if not self.ltm:
            return "群聊记忆模块尚未初始化。"
        return await self.ltm.search_group_history(
            event,
            query=query,
            limit=limit,
            hours=hours,
        )
