#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "runtime" / "codex-prod" / "data" / "cmd_config.json"
LOG = logging.getLogger("qq_stack_boot_listener")

START_COMMANDS = {
    "/stack 启动",
    "/stack 打开",
    "/stack 开机",
}
STATUS_COMMANDS = {
    "/stack 状态",
}


class QQStackBootListener:
    def __init__(self) -> None:
        self.root_dir = ROOT_DIR
        self.codex_prod_manage = self.root_dir / "scripts" / "manage_codex_prod.sh"
        self.doubao_bridge_manage = (
            self.root_dir / "scripts" / "manage_doubao_bridge.sh"
        )

    def load_admin_ids(self) -> set[str]:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            LOG.exception("Failed to load admin IDs from %s", CONFIG_PATH)
            return set()

        return {str(item) for item in data.get("admins_id", [])}

    def parse_plain_text(self, payload: dict[str, Any]) -> str:
        message = payload.get("message", [])
        if not isinstance(message, list):
            return ""

        texts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") != "text":
                continue
            data = segment.get("data", {})
            if not isinstance(data, dict):
                continue
            text = data.get("text", "")
            if isinstance(text, str):
                texts.append(text)
        return "".join(texts).strip()

    def stack_running(self) -> bool:
        codex_status = self.run_script(self.codex_prod_manage, "status")
        doubao_status = self.run_script(self.doubao_bridge_manage, "status")
        return codex_status.returncode == 0 and doubao_status.returncode == 0

    def run_script(
        self, script_path: Path, action: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script_path), action],
            cwd=self.root_dir,
            text=True,
            capture_output=True,
        )

    async def maybe_handle_command(
        self, ws: web.WebSocketResponse, payload: dict[str, Any]
    ) -> None:
        if payload.get("post_type") != "message":
            return
        if payload.get("message_type") != "private":
            return

        user_id = str(payload.get("user_id", ""))
        if user_id not in self.load_admin_ids():
            return

        text = self.parse_plain_text(payload)
        if not text:
            return

        running = await asyncio.to_thread(self.stack_running)

        if text in STATUS_COMMANDS and not running:
            await self.send_private_msg(
                ws,
                user_id,
                "当前服务是关闭状态。发 /stack 启动 可以把它拉起来。",
            )
            return

        if text in START_COMMANDS and not running:
            await self.send_private_msg(
                ws,
                user_id,
                "收到启动命令，正在拉起 AstrBot 和豆包桥。",
            )
            await asyncio.to_thread(self.run_script, self.doubao_bridge_manage, "start")
            await asyncio.to_thread(self.run_script, self.codex_prod_manage, "start")
            await asyncio.sleep(2)
            await self.send_private_msg(
                ws,
                user_id,
                "启动命令已执行。稍等几秒后你再发消息试试。",
            )

    async def send_private_msg(
        self, ws: web.WebSocketResponse, user_id: str, text: str
    ) -> None:
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": int(user_id),
                "message": [
                    {
                        "type": "text",
                        "data": {"text": text},
                    }
                ],
            },
            "echo": f"stack-boot-{asyncio.get_running_loop().time()}",
        }
        await ws.send_json(payload)

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        LOG.info("NapCat control websocket connected from %s", request.remote)

        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    await self.maybe_handle_command(ws, payload)
            elif message.type == WSMsgType.ERROR:
                LOG.warning(
                    "Control websocket closed with exception: %s", ws.exception()
                )

        LOG.info("NapCat control websocket disconnected from %s", request.remote)
        return ws


def build_app() -> web.Application:
    listener = QQStackBootListener()
    app = web.Application()
    app.router.add_get("/ws", listener.websocket_handler)
    return app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = "0.0.0.0"
    port = 6200
    LOG.info("Starting QQ stack boot listener on ws://%s:%s/ws", host, port)
    web.run_app(build_app(), host=host, port=port, handle_signals=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
