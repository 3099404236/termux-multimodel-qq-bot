import asyncio
import os
import subprocess
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult


class StackControlCommands:
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.root_dir = Path(__file__).resolve().parents[4]
        self.codex_prod_manage = self.root_dir / "scripts" / "manage_codex_prod.sh"
        self.doubao_bridge_manage = (
            self.root_dir / "scripts" / "manage_doubao_bridge.sh"
        )

    async def stack(self, event: AstrMessageEvent, action: str = "") -> None:
        if not event.is_private_chat():
            self._finish(event, "这个命令只允许私聊使用。")
            return

        normalized = action.strip().lower()
        if not normalized:
            self._finish(
                event,
                "用法: /stack 状态 | 关闭 | 重启 | 启动 | 主动回复 状态|开启|关闭",
            )
            return

        if normalized in {
            "主动回复 状态",
            "主动回复 查询",
            "主动回复 查看",
            "跟进 状态",
            "跟进 查询",
            "跟进 查看",
        }:
            await self._active_reply_status(event)
            return

        if normalized in {
            "主动回复 开启",
            "主动回复 打开",
            "主动回复 启用",
            "跟进 开启",
            "跟进 打开",
            "跟进 启用",
        }:
            await self._set_active_reply(event, True)
            return

        if normalized in {
            "主动回复 关闭",
            "主动回复 禁用",
            "主动回复 停用",
            "跟进 关闭",
            "跟进 禁用",
            "跟进 停用",
        }:
            await self._set_active_reply(event, False)
            return

        if normalized in {"status", "状态"}:
            await self._status(event)
            return

        if normalized in {"stop", "关闭", "关机", "停止"}:
            self._finish(event, "收到。2 秒后关闭 AstrBot 和豆包桥。")
            self._launch_background_sequence(
                "sleep 2\n"
                "./scripts/manage_codex_prod.sh stop\n"
                "./scripts/manage_doubao_bridge.sh stop\n",
            )
            return

        if normalized in {"restart", "重启"}:
            self._finish(event, "收到。2 秒后重启 AstrBot 和豆包桥。")
            self._launch_background_sequence(
                "sleep 2\n"
                "./scripts/manage_codex_prod.sh stop\n"
                "./scripts/manage_doubao_bridge.sh stop\n"
                "sleep 1\n"
                "./scripts/manage_doubao_bridge.sh start\n"
                "./scripts/manage_codex_prod.sh start\n",
            )
            return

        if normalized in {"start", "启动", "打开", "开机"}:
            await self._start(event)
            return

        self._finish(
            event,
            "不支持这个动作。可用动作: 状态 / 关闭 / 重启 / 启动 / 主动回复 状态|开启|关闭",
        )

    async def _status(self, event: AstrMessageEvent) -> None:
        codex_status = await asyncio.to_thread(
            self._run_script,
            self.codex_prod_manage,
            "status",
        )
        doubao_status = await asyncio.to_thread(
            self._run_script,
            self.doubao_bridge_manage,
            "status",
        )

        parts = [
            "当前服务状态：",
            (codex_status.stdout or codex_status.stderr).strip()
            or "codex-prod: 无输出",
            (doubao_status.stdout or doubao_status.stderr).strip()
            or "doubao-bridge: 无输出",
            "",
            "说明：/stack 启动 只有在我还在线时才能收到；如果我已经关了，请直接用控制页打开。",
        ]
        self._finish(event, "\n".join(parts))

    async def _start(self, event: AstrMessageEvent) -> None:
        doubao_result = await asyncio.to_thread(
            self._run_script,
            self.doubao_bridge_manage,
            "start",
        )
        codex_result = await asyncio.to_thread(
            self._run_script,
            self.codex_prod_manage,
            "start",
        )

        if doubao_result.returncode == 0 and codex_result.returncode == 0:
            self._finish(event, "启动命令已执行。")
            return

        details = "\n".join(
            filter(
                None,
                [
                    (doubao_result.stdout or doubao_result.stderr).strip(),
                    (codex_result.stdout or codex_result.stderr).strip(),
                ],
            )
        )
        self._finish(event, f"启动命令执行失败。\n{details or '没有拿到更多输出。'}")

    async def _active_reply_status(self, event: AstrMessageEvent) -> None:
        cfg = self.context.get_config()
        active_reply_cfg = cfg["provider_ltm_settings"]["active_reply"]
        enabled = bool(active_reply_cfg.get("enable", False))
        method = active_reply_cfg.get("method", "unknown")
        status_text = "开启" if enabled else "关闭"
        self._finish(
            event,
            f"当前默认配置的群聊跟进回复：{status_text}\n触发方式：{method}\n用法：/stack 主动回复 状态|开启|关闭",
        )

    async def _set_active_reply(self, event: AstrMessageEvent, enabled: bool) -> None:
        cfg = self.context.get_config()
        active_reply_cfg = cfg["provider_ltm_settings"]["active_reply"]
        current = bool(active_reply_cfg.get("enable", False))
        if current == enabled:
            self._finish(
                event,
                f"默认配置的群聊跟进回复已经是{'开启' if enabled else '关闭'}状态。",
            )
            return

        active_reply_cfg["enable"] = enabled
        cfg.save_config()
        self._finish(
            event, f"已{'开启' if enabled else '关闭'}默认配置的群聊跟进回复。"
        )

    def _finish(self, event: AstrMessageEvent, text: str) -> None:
        event.should_call_llm(False)
        event.set_result(MessageEventResult().message(text))
        event.stop_event()

    def _launch_background_sequence(self, script_text: str) -> None:
        subprocess.Popen(
            ["/bin/bash", "-lc", script_text],
            cwd=self.root_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

    def _run_script(
        self, script_path: Path, action: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script_path), action],
            cwd=self.root_dir,
            text=True,
            capture_output=True,
        )
