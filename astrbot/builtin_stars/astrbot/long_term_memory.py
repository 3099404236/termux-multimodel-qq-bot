import asyncio
import datetime
import json
import random
import re
import time
import uuid
from collections import defaultdict

from astrbot import logger
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, Provider, ProviderRequest
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager

from .group_memory import GroupMemoryStore, build_summary_prompt

"""
聊天记忆增强
"""

ACTIVE_REPLY_COOLDOWN_SECONDS = 45.0
ACTIVE_REPLY_RECENT_BOT_WINDOW_SECONDS = 180.0
ACTIVE_REPLY_FOLLOW_UP_WINDOW_SECONDS = 90.0
ACTIVE_REPLY_REQUEST_EXTRA_KEY = "_active_reply_request"
ACTIVE_REPLY_NO_REPLY_TOKEN = "__ASTRBOT_NO_REPLY__"
ACTIVE_REPLY_NO_REPLY_NORMALIZED_VARIANTS = {
    "ASTRBOTNOREPLY",
    "NOREPLY",
}
GROUP_MEMORY_MESSAGE_ID_EXTRA_KEY = "_group_memory_message_id"
GROUP_MEMORY_DEFAULT_RECENT_MESSAGES = 18
GROUP_MEMORY_DEFAULT_SUMMARY_BATCH_SIZE = 30
GROUP_MEMORY_DEFAULT_SUMMARY_COUNT = 3
GROUP_MEMORY_DEFAULT_QUIET_SECONDS = 90
ACTIVE_REPLY_LITERAL_RESPONSE_GUIDANCE = (
    "Stay grounded in the observable message and recent chat history.\n"
    "Do not over-interpret short reactive remarks into a broader trend, motive, or state.\n"
    "For brief updates about prices, trends, or status changes such as '又跌了', '又涨了', '又卡了', or '又崩了', respond literally unless the history explicitly supports a stronger conclusion.\n"
    "Do not judge a price as cheap, expensive, fair, or outrageous unless the recent history explicitly gives enough basis for that judgment.\n"
    "If the basis is unclear, ask for the missing context or say you are not sure."
)
ACTIVE_REPLY_ABSTAIN_GUIDANCE = (
    "If joining the conversation would add little value, or you still do not have enough basis to answer naturally after using the recent history, output exactly "
    f"{ACTIVE_REPLY_NO_REPLY_TOKEN}."
)

BOT_REFERENCE_KEYWORDS = (
    "机器人",
    "助手",
    "助理",
    "astrbot",
    "codex",
)

QUESTION_HINTS = (
    "?",
    "？",
    "吗",
    "么",
    "嘛",
    "怎么",
    "怎样",
    "如何",
    "为啥",
    "为什么",
    "是否",
    "能不能",
    "可不可以",
    "行不行",
    "谁",
    "什么",
    "哪里",
    "哪儿",
    "咋",
)

REQUEST_HINTS = (
    "帮我",
    "帮忙",
    "麻烦",
    "请",
    "请问",
    "解释",
    "分析",
    "总结",
    "翻译",
    "看看",
    "看下",
    "看一下",
    "说说",
    "告诉我",
    "教我",
    "推荐",
    "建议",
    "写个",
    "写一段",
)

FOLLOW_UP_HINTS = (
    "还是",
    "依旧",
    "仍然",
    "没用",
    "不行",
    "不对",
    "不生效",
    "第一次",
    "刚打开",
    "刚装",
    "我这边",
    "我这里",
    "这里",
    "然后",
    "结果",
    "但是",
    "不过",
    "开了",
    "关了",
    "试了",
    "设置",
    "报错",
    "渲染",
    "字体",
    "背景",
    "色块",
    "显示",
    "gpu",
    "cpu",
    "vscode",
    "docker",
    "off",
    "on",
)

LOW_SIGNAL_TEXTS = {
    "?",
    "？",
    "1",
    "2",
    "3",
    "6",
    "66",
    "666",
    "ok",
    "okay",
    "okok",
    "收到",
    "好的",
    "嗯",
    "嗯嗯",
    "啊",
    "哦",
    "在吗",
    "在么",
    "哈哈",
    "哈哈哈",
    "hhh",
    "hh",
}


class LongTermMemory:
    def __init__(self, acm: AstrBotConfigManager, context: star.Context) -> None:
        self.acm = acm
        self.context = context
        self.session_chats = defaultdict(list)
        """记录群成员的群聊记录"""
        self.last_bot_reply_at = defaultdict(float)
        """Track recent bot participation per chatroom."""
        self.last_bot_reply_sender_id = {}
        """Track which sender the bot most recently replied to per chatroom."""
        self.last_active_reply_at = defaultdict(float)
        """Rate-limit proactive replies per chatroom."""
        self.follow_up_sender_id = {}
        """Track which sender currently owns the follow-up window per chatroom."""
        self.group_memory = GroupMemoryStore()
        self.group_memory_tasks: dict[str, asyncio.Task] = {}
        self.group_memory_last_activity = defaultdict(float)

    def cfg(self, event: AstrMessageEvent):
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        try:
            max_cnt = int(cfg["provider_ltm_settings"]["group_message_max_cnt"])
        except BaseException as e:
            logger.error(e)
            max_cnt = 300
        image_caption_prompt = cfg["provider_settings"]["image_caption_prompt"]
        image_caption_provider_id = cfg["provider_ltm_settings"].get(
            "image_caption_provider_id"
        )
        image_caption = cfg["provider_ltm_settings"]["image_caption"] and bool(
            image_caption_provider_id
        )
        active_reply = cfg["provider_ltm_settings"]["active_reply"]
        enable_active_reply = active_reply.get("enable", False)
        ar_method = active_reply["method"]
        ar_possibility = active_reply["possibility_reply"]
        ar_prompt = active_reply.get("prompt", "")
        ar_whitelist = active_reply.get("whitelist", [])
        ret = {
            "max_cnt": max_cnt,
            "image_caption": image_caption,
            "image_caption_prompt": image_caption_prompt,
            "image_caption_provider_id": image_caption_provider_id,
            "enable_active_reply": enable_active_reply,
            "ar_method": ar_method,
            "ar_possibility": ar_possibility,
            "ar_prompt": ar_prompt,
            "ar_whitelist": ar_whitelist,
        }
        return ret

    def memory_cfg(self, event: AstrMessageEvent) -> dict:
        config = self.context.get_config(umo=event.unified_msg_origin)
        memory_config = config["provider_ltm_settings"].get("group_memory", {})
        return {
            "enable": memory_config.get("enable", True),
            "recent_messages": max(
                int(
                    memory_config.get(
                        "recent_messages", GROUP_MEMORY_DEFAULT_RECENT_MESSAGES
                    )
                ),
                1,
            ),
            "summary_batch_size": max(
                int(
                    memory_config.get(
                        "summary_batch_size",
                        GROUP_MEMORY_DEFAULT_SUMMARY_BATCH_SIZE,
                    )
                ),
                5,
            ),
            "summary_count": max(
                int(
                    memory_config.get(
                        "summary_count", GROUP_MEMORY_DEFAULT_SUMMARY_COUNT
                    )
                ),
                0,
            ),
            "quiet_seconds": max(
                int(
                    memory_config.get(
                        "quiet_seconds", GROUP_MEMORY_DEFAULT_QUIET_SECONDS
                    )
                ),
                10,
            ),
            "compress_provider_id": str(
                memory_config.get("compress_provider_id", "")
            ).strip(),
        }

    def mark_active_reply_request(self, event: AstrMessageEvent) -> None:
        event.set_extra(ACTIVE_REPLY_REQUEST_EXTRA_KEY, True)

    def is_active_reply_request(self, event: AstrMessageEvent) -> bool:
        return bool(event.get_extra(ACTIVE_REPLY_REQUEST_EXTRA_KEY, False))

    def suppress_active_reply_response_if_needed(
        self, event: AstrMessageEvent, llm_resp: LLMResponse
    ) -> bool:
        if not self.is_active_reply_request(event):
            return False
        if not self._is_no_reply_response(llm_resp.completion_text or ""):
            return False

        llm_resp.result_chain = None
        llm_resp._completion_text = ""
        llm_resp.reasoning_content = ""
        logger.debug(
            f"active_reply | suppressed | {event.unified_msg_origin} | no_reply_token"
        )
        return True

    async def remove_session(self, event: AstrMessageEvent) -> int:
        cnt = 0
        if event.unified_msg_origin in self.session_chats:
            cnt = len(self.session_chats[event.unified_msg_origin])
            del self.session_chats[event.unified_msg_origin]
        self.last_bot_reply_at.pop(event.unified_msg_origin, None)
        self.last_bot_reply_sender_id.pop(event.unified_msg_origin, None)
        self.last_active_reply_at.pop(event.unified_msg_origin, None)
        self.follow_up_sender_id.pop(event.unified_msg_origin, None)
        return cnt

    async def get_image_caption(
        self,
        image_url: str,
        image_caption_provider_id: str,
        image_caption_prompt: str,
    ) -> str:
        if not image_caption_provider_id:
            provider = self.context.get_using_provider()
        else:
            provider = self.context.get_provider_by_id(image_caption_provider_id)
            if not provider:
                raise Exception(f"没有找到 ID 为 {image_caption_provider_id} 的提供商")
        if not isinstance(provider, Provider):
            raise Exception(f"提供商类型错误({type(provider)})，无法获取图片描述")
        response = await provider.text_chat(
            prompt=image_caption_prompt,
            session_id=uuid.uuid4().hex,
            image_urls=[image_url],
            persist=False,
        )
        return response.completion_text

    async def need_active_reply(self, event: AstrMessageEvent) -> bool:
        cfg = self.cfg(event)
        if not cfg["enable_active_reply"]:
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return False

        if event.is_at_or_wake_command:
            # if the message is a command, let it pass
            return False

        if cfg["ar_whitelist"] and (
            event.unified_msg_origin not in cfg["ar_whitelist"]
            and (
                event.get_group_id() and event.get_group_id() not in cfg["ar_whitelist"]
            )
        ):
            return False

        match cfg["ar_method"]:
            case "possibility_reply":
                trig = random.random() < cfg["ar_possibility"]
                if trig:
                    self.last_active_reply_at[event.unified_msg_origin] = (
                        time.monotonic()
                    )
                return trig
            case "rule_based_reply":
                trig, reason = self._should_trigger_rule_based_reply(event)
                if trig:
                    self.last_active_reply_at[event.unified_msg_origin] = (
                        time.monotonic()
                    )
                    logger.debug(
                        f"active_reply | triggered | {event.unified_msg_origin} | {reason}"
                    )
                else:
                    logger.debug(
                        f"active_reply | skipped | {event.unified_msg_origin} | {reason}"
                    )
                return trig

        return False

    def _is_explicit_group_invocation(self, event: AstrMessageEvent) -> bool:
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False

        for comp in event.get_messages():
            if isinstance(comp, At) and str(comp.qq) == str(event.get_self_id()):
                return True
            if isinstance(comp, Reply) and str(comp.sender_id) == str(
                event.get_self_id()
            ):
                return True

        return False

    def _should_trigger_rule_based_reply(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str]:
        now = time.monotonic()
        last_active_reply_at = self.last_active_reply_at.get(
            event.unified_msg_origin, 0
        )
        cooldown_seconds = now - last_active_reply_at

        text = (event.message_str or "").strip()
        text_lower = text.lower()
        has_image = any(isinstance(comp, Image) for comp in event.get_messages())
        replies_to_bot = any(
            isinstance(comp, Reply)
            and str(comp.sender_id)
            and str(comp.sender_id) == str(event.get_self_id())
            for comp in event.get_messages()
        )

        if self._looks_like_control_or_status_message(text, text_lower):
            return False, "control_or_status_message"

        if self._is_low_signal_text(text) and not has_image:
            return False, "low_signal"

        asks_question = self._looks_like_question(text_lower)
        requests_help = self._looks_like_request(text_lower)
        recent_bot_reply = (
            now - self.last_bot_reply_at.get(event.unified_msg_origin, 0)
            < ACTIVE_REPLY_RECENT_BOT_WINDOW_SECONDS
        )
        recent_same_sender_follow_up = (
            recent_bot_reply
            and now - self.last_bot_reply_at.get(event.unified_msg_origin, 0)
            < ACTIVE_REPLY_FOLLOW_UP_WINDOW_SECONDS
            and str(event.get_sender_id())
            == self.follow_up_sender_id.get(event.unified_msg_origin, "")
        )
        looks_like_follow_up = self._looks_like_follow_up(text_lower)
        continuation_follow_up = recent_same_sender_follow_up and (
            looks_like_follow_up or asks_question or requests_help or replies_to_bot
        )

        if (
            last_active_reply_at
            and cooldown_seconds < ACTIVE_REPLY_COOLDOWN_SECONDS
            and not continuation_follow_up
        ):
            return False, f"cooldown:{cooldown_seconds:.1f}s"

        if not recent_same_sender_follow_up:
            return False, "follow_up_window_closed"
        if not continuation_follow_up:
            return False, "not_follow_up_like"

        reason_parts = []
        if replies_to_bot:
            reason_parts.append("replies_to_bot")
        if recent_bot_reply:
            reason_parts.append("recent_bot_reply")
        if asks_question:
            reason_parts.append("asks_question")
        if requests_help:
            reason_parts.append("requests_help")
        if continuation_follow_up:
            reason_parts.append("same_sender_follow_up")
        if has_image:
            reason_parts.append("has_image")

        return True, ",".join(reason_parts)

    def _mentions_bot(self, event: AstrMessageEvent, text_lower: str) -> bool:
        if any(keyword in text_lower for keyword in BOT_REFERENCE_KEYWORDS):
            return True

        if re.search(r"\b(bot|ai|assistant)\b", text_lower):
            return True

        self_id = str(event.get_self_id()).strip().lower()
        if self_id and not self_id.isdigit() and self_id in text_lower:
            return True

        return False

    def _looks_like_question(self, text_lower: str) -> bool:
        if not text_lower:
            return False
        if any(keyword in text_lower for keyword in QUESTION_HINTS):
            return True
        return bool(
            re.search(
                r"\b(what|why|how|who|when|where|which|can|could|would|should)\b",
                text_lower,
            )
        )

    def _looks_like_request(self, text_lower: str) -> bool:
        if not text_lower:
            return False
        if any(keyword in text_lower for keyword in REQUEST_HINTS):
            return True
        return bool(
            re.search(r"\b(help|explain|summarize|translate|write)\b", text_lower)
        )

    def _looks_like_follow_up(self, text_lower: str) -> bool:
        if not text_lower:
            return False
        if len(text_lower.strip()) < 4:
            return False
        if any(keyword in text_lower for keyword in FOLLOW_UP_HINTS):
            return True
        return False

    def _looks_like_control_or_status_message(
        self, text: str, text_lower: str | None = None
    ) -> bool:
        normalized = text.strip()
        if not normalized:
            return False

        if text_lower is None:
            text_lower = normalized.lower()

        if re.match(r"^/[A-Za-z0-9_-]+(?:\s.*)?$", normalized):
            return True
        if re.match(r"^#{1,3}\S.*$", normalized):
            return True
        if re.match(
            r"^napcat\s*(信息|状态|帮助|设置|菜单|help|status|info)\b",
            text_lower,
        ):
            return True
        return False

    def _normalize_no_reply_signal(self, text: str) -> str:
        return re.sub(r"[\s`*_~'\"“”‘’<>\-.,!?:;()\[\]{}]+", "", text).upper()

    def _is_no_reply_response(self, text: str) -> bool:
        normalized_full = self._normalize_no_reply_signal(text)
        if normalized_full in ACTIVE_REPLY_NO_REPLY_NORMALIZED_VARIANTS:
            return True

        lines = [
            self._normalize_no_reply_signal(line)
            for line in text.splitlines()
            if line.strip()
        ]
        if lines and all(
            line in ACTIVE_REPLY_NO_REPLY_NORMALIZED_VARIANTS for line in lines
        ):
            return True

        return False

    def _is_low_signal_text(self, text: str) -> bool:
        normalized = text.strip().lower()
        if not normalized:
            return True
        if normalized in LOW_SIGNAL_TEXTS:
            return True
        if len(normalized) <= 1:
            return True
        if re.fullmatch(r"[\W_]+", normalized):
            return True
        if len(set(normalized)) == 1 and len(normalized) <= 6:
            return True
        return False

    async def _record_group_memory(
        self,
        event: AstrMessageEvent,
        *,
        role: str,
        sender_id: str,
        sender_name: str,
        content: str,
    ) -> int | None:
        memory_config = self.memory_cfg(event)
        if not memory_config["enable"]:
            return None
        try:
            message_id = await asyncio.to_thread(
                self.group_memory.record_message,
                umo=event.unified_msg_origin,
                role=role,
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
            )
        except Exception as exc:
            logger.error(f"group_memory | failed to record message: {exc}")
            return None

        self._schedule_group_memory_compression(event, memory_config)
        return message_id

    def _schedule_group_memory_compression(
        self,
        event: AstrMessageEvent,
        memory_config: dict,
    ) -> None:
        umo = event.unified_msg_origin
        self.group_memory_last_activity[umo] = time.monotonic()
        existing_task = self.group_memory_tasks.get(umo)
        if existing_task and not existing_task.done():
            return
        self.group_memory_tasks[umo] = asyncio.create_task(
            self._compress_group_memory_when_quiet(event, memory_config)
        )

    async def _compress_group_memory_when_quiet(
        self,
        event: AstrMessageEvent,
        memory_config: dict,
    ) -> None:
        umo = event.unified_msg_origin
        quiet_seconds = memory_config["quiet_seconds"]
        try:
            while True:
                elapsed = time.monotonic() - self.group_memory_last_activity[umo]
                remaining = quiet_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                break

            batch = await asyncio.to_thread(
                self.group_memory.next_summary_batch,
                umo,
                memory_config["summary_batch_size"],
            )
            if not batch:
                return

            provider_id = memory_config["compress_provider_id"]
            provider = (
                self.context.get_provider_by_id(provider_id)
                if provider_id
                else self.context.get_using_provider(umo)
            )
            if not isinstance(provider, Provider):
                logger.warning(
                    f"group_memory | compressor provider unavailable | {umo}"
                )
                return

            response = await provider.text_chat(
                prompt=build_summary_prompt(batch),
                session_id=(
                    f"group-memory-compressor:{umo}:"
                    f"{batch.start_message_id}-{batch.end_message_id}"
                ),
                contexts=[],
                system_prompt=(
                    "You compress chat history into evidence-grounded structured "
                    "memory. Never invent facts and output only the requested JSON."
                ),
            )
            summary = (response.completion_text or "").strip()
            if not summary:
                logger.warning(f"group_memory | compressor returned empty | {umo}")
                return
            summary = self._normalize_structured_summary(summary)
            await asyncio.to_thread(
                self.group_memory.save_summary,
                umo,
                batch,
                summary,
            )
            logger.info(
                "group_memory | compressed | %s | messages=%s range=%s-%s",
                umo,
                len(batch.messages),
                batch.start_message_id,
                batch.end_message_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"group_memory | compression failed | {umo} | {exc}")
        finally:
            current_task = asyncio.current_task()
            if self.group_memory_tasks.get(umo) is current_task:
                self.group_memory_tasks.pop(umo, None)

    @staticmethod
    def _normalize_structured_summary(summary: str) -> str:
        candidate = summary.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return summary
        if not isinstance(parsed, dict):
            return summary
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))

    async def search_group_history(
        self,
        event: AstrMessageEvent,
        *,
        query: str,
        limit: int,
        hours: int,
    ) -> str:
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return "该工具只能搜索当前群聊的历史记录。"
        memory_config = self.memory_cfg(event)
        if not memory_config["enable"]:
            return "当前群聊没有启用持久化记忆。"
        return await asyncio.to_thread(
            self.group_memory.search,
            event.unified_msg_origin,
            query=query,
            limit=limit,
            hours=hours,
        )

    async def handle_message(self, event: AstrMessageEvent) -> None:
        """仅支持群聊"""
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            text = (event.message_str or "").strip()
            if self._looks_like_control_or_status_message(text):
                logger.debug(
                    f"ltm | skipped control/status message | {event.unified_msg_origin} | {text}"
                )
                return

            datetime_str = datetime.datetime.now().strftime("%H:%M:%S")

            parts = [f"[{event.message_obj.sender.nickname}/{datetime_str}]: "]

            cfg = self.cfg(event)

            for comp in event.get_messages():
                if isinstance(comp, Plain):
                    parts.append(f" {comp.text}")
                elif isinstance(comp, Image):
                    if cfg["image_caption"]:
                        try:
                            url = comp.url if comp.url else comp.file
                            if not url:
                                raise Exception("图片 URL 为空")
                            caption = await self.get_image_caption(
                                url,
                                cfg["image_caption_provider_id"],
                                cfg["image_caption_prompt"],
                            )
                            parts.append(f" [Image: {caption}]")
                        except Exception as e:
                            logger.error(f"获取图片描述失败: {e}")
                    else:
                        parts.append(" [Image]")
                elif isinstance(comp, At):
                    parts.append(f" [At: {comp.name}]")

            final_message = "".join(parts)
            logger.debug(f"ltm | {event.unified_msg_origin} | {final_message}")
            self.session_chats[event.unified_msg_origin].append(final_message)
            event.set_extra("_ltm_recorded_message", final_message)
            memory_message_id = await self._record_group_memory(
                event,
                role="user",
                sender_id=str(event.get_sender_id()),
                sender_name=str(event.message_obj.sender.nickname or ""),
                content=final_message,
            )
            if memory_message_id is not None:
                event.set_extra(
                    GROUP_MEMORY_MESSAGE_ID_EXTRA_KEY,
                    memory_message_id,
                )
            if len(self.session_chats[event.unified_msg_origin]) > cfg["max_cnt"]:
                self.session_chats[event.unified_msg_origin].pop(0)

    async def on_req_llm(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """当触发 LLM 请求前，调用此方法修改 req"""
        if event.unified_msg_origin not in self.session_chats:
            return

        cfg = self.cfg(event)
        chats = list(self.session_chats[event.unified_msg_origin])
        memory_config = self.memory_cfg(event)
        if cfg["enable_active_reply"]:
            recorded_message = event.get_extra("_ltm_recorded_message", "")
            if recorded_message and chats and chats[-1] == recorded_message:
                chats = chats[:-1]

        chats_str = "\n---\n".join(chats)
        if memory_config["enable"]:
            try:
                persistent_context = await asyncio.to_thread(
                    self.group_memory.render_context,
                    event.unified_msg_origin,
                    recent_limit=max(
                        memory_config["recent_messages"],
                        memory_config["summary_batch_size"],
                    ),
                    summary_limit=memory_config["summary_count"],
                    exclude_message_id=(
                        event.get_extra(GROUP_MEMORY_MESSAGE_ID_EXTRA_KEY, None)
                        if cfg["enable_active_reply"]
                        else None
                    ),
                )
                if persistent_context:
                    chats_str = persistent_context
            except Exception as exc:
                logger.error(f"group_memory | failed to build context: {exc}")
        if cfg["enable_active_reply"] and self.is_active_reply_request(event):
            prompt = req.prompt
            prompt_sections = []
            if chats_str:
                prompt_sections.append(
                    "You are now in a chatroom. The recent chat history is as follows:\n"
                    f"{chats_str}"
                )
            else:
                prompt_sections.append(
                    "You are now in a chatroom with no recent history available."
                )
            prompt_sections.append(
                f"Now, a new message is coming: `{prompt}`.\n"
                "The bot was explicitly addressed earlier, and this new message may be a follow-up from the same person.\n"
                "Only continue if this clearly looks like that person's follow-up or added detail.\n"
                "Only output your response and do not output any other information.\n"
                "You MUST use the SAME language as the chatroom is using."
            )
            prompt_sections.append(ACTIVE_REPLY_LITERAL_RESPONSE_GUIDANCE)
            prompt_sections.append(ACTIVE_REPLY_ABSTAIN_GUIDANCE)
            if cfg["ar_prompt"]:
                prompt_sections.append(f"Additional instruction:\n{cfg['ar_prompt']}")
            req.prompt = "\n\n".join(prompt_sections)
            req.contexts = []  # 清空上下文，当使用了主动回复，所有聊天记录都在一个prompt中。
        else:
            req.system_prompt += (
                "You are now in a chatroom. The chat history is as follows: \n"
            )
            req.system_prompt += chats_str

    async def after_req_llm(
        self, event: AstrMessageEvent, llm_resp: LLMResponse
    ) -> None:
        if event.unified_msg_origin not in self.session_chats:
            return

        if llm_resp.completion_text:
            if event.get_message_type() == MessageType.GROUP_MESSAGE:
                self.last_bot_reply_at[event.unified_msg_origin] = time.monotonic()
                self.last_bot_reply_sender_id[event.unified_msg_origin] = str(
                    event.get_sender_id()
                )
                if self._is_explicit_group_invocation(
                    event
                ) or self.is_active_reply_request(event):
                    self.follow_up_sender_id[event.unified_msg_origin] = str(
                        event.get_sender_id()
                    )
                else:
                    self.follow_up_sender_id.pop(event.unified_msg_origin, None)
            final_message = f"[You/{datetime.datetime.now().strftime('%H:%M:%S')}]: {llm_resp.completion_text}"
            logger.debug(
                f"Recorded AI response: {event.unified_msg_origin} | {final_message}"
            )
            self.session_chats[event.unified_msg_origin].append(final_message)
            await self._record_group_memory(
                event,
                role="assistant",
                sender_id=str(event.get_self_id()),
                sender_name="You",
                content=final_message,
            )
            cfg = self.cfg(event)
            if len(self.session_chats[event.unified_msg_origin]) > cfg["max_cnt"]:
                self.session_chats[event.unified_msg_origin].pop(0)
