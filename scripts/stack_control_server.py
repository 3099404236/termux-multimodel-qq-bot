#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = ROOT_DIR / "runtime" / "stack-control"


def probe_url(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def tail_text(path: Path, line_count: int = 40) -> str:
    if not path.exists():
        return ""

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    return "".join(lines[-line_count:])


class StackController:
    def __init__(self, runtime_root: Path, host: str, port: int):
        self.runtime_root = runtime_root
        self.host = host
        self.port = port
        self.token_file = runtime_root / "token"
        self.log_dir = runtime_root / "logs"
        self.action_log_file = self.log_dir / "stack-actions.log"
        self.astrbot_log_file = (
            ROOT_DIR / "runtime" / "codex-prod" / "logs" / "astrbot.log"
        )
        self.bridge_log_file = (
            ROOT_DIR / "runtime" / "doubao-prod" / "logs" / "doubao-bridge.log"
        )
        self.codex_prod_manage = ROOT_DIR / "scripts" / "manage_codex_prod.sh"
        self.doubao_bridge_manage = ROOT_DIR / "scripts" / "manage_doubao_bridge.sh"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.token = self._load_or_create_token()

    def _load_or_create_token(self) -> str:
        if self.token_file.exists():
            token = self.token_file.read_text(encoding="utf-8").strip()
            if token:
                return token

        token = secrets.token_urlsafe(24)
        self.token_file.write_text(token, encoding="utf-8")
        return token

    def _run_script(
        self, script_path: Path, action: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script_path), action],
            cwd=ROOT_DIR,
            text=True,
            capture_output=True,
        )

    def _append_action_log(
        self, label: str, result: subprocess.CompletedProcess[str]
    ) -> None:
        with self.action_log_file.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label} rc={result.returncode}\n"
            )
            if result.stdout:
                handle.write(result.stdout)
                if not result.stdout.endswith("\n"):
                    handle.write("\n")
            if result.stderr:
                handle.write(result.stderr)
                if not result.stderr.endswith("\n"):
                    handle.write("\n")

    def _wait_for_state(self, *, up: bool, timeout_seconds: int = 30) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            astrbot_ready = probe_url("http://127.0.0.1:6185")
            bridge_ready = probe_url("http://127.0.0.1:8790/healthz")
            if up and astrbot_ready and bridge_ready:
                return True
            if not up and not astrbot_ready and not bridge_ready:
                return True
            time.sleep(1)
        return False

    def start(self) -> tuple[bool, str]:
        bridge_result = self._run_script(self.doubao_bridge_manage, "start")
        astrbot_result = self._run_script(self.codex_prod_manage, "start")
        self._append_action_log("doubao-bridge start", bridge_result)
        self._append_action_log("codex-prod start", astrbot_result)

        ok = bridge_result.returncode == 0 and astrbot_result.returncode == 0
        if ok:
            self._wait_for_state(up=True)
        return (
            ok,
            "Started the managed AstrBot stack."
            if ok
            else "Failed to start the managed AstrBot stack.",
        )

    def stop(self) -> tuple[bool, str]:
        astrbot_result = self._run_script(self.codex_prod_manage, "stop")
        bridge_result = self._run_script(self.doubao_bridge_manage, "stop")
        self._append_action_log("codex-prod stop", astrbot_result)
        self._append_action_log("doubao-bridge stop", bridge_result)

        ok = bridge_result.returncode == 0 and astrbot_result.returncode == 0
        if ok:
            self._wait_for_state(up=False)
        return (
            ok,
            "Stopped the managed AstrBot stack."
            if ok
            else "Failed to stop the managed AstrBot stack.",
        )

    def restart(self) -> tuple[bool, str]:
        stopped_ok, stopped_message = self.stop()
        if not stopped_ok:
            return False, stopped_message

        started_ok, started_message = self.start()
        if not started_ok:
            return False, started_message

        return True, f"{stopped_message} {started_message}"

    def status(self) -> dict[str, object]:
        doubao_status = self._run_script(self.doubao_bridge_manage, "status")
        codex_status = self._run_script(self.codex_prod_manage, "status")
        stack_running = doubao_status.returncode == 0 and codex_status.returncode == 0
        return {
            "stack_running": stack_running,
            "astrbot_webui_ready": probe_url("http://127.0.0.1:6185"),
            "doubao_bridge_ready": probe_url("http://127.0.0.1:8790/healthz"),
            "control_host": self.host,
            "control_port": self.port,
            "control_token": self.token,
            "stack_log": str(self.action_log_file),
            "astrbot_log": str(self.astrbot_log_file),
            "bridge_log": str(self.bridge_log_file),
            "doubao_status": (doubao_status.stdout or doubao_status.stderr).strip(),
            "codex_prod_status": (codex_status.stdout or codex_status.stderr).strip(),
        }


def render_page(
    controller: StackController, request_host: str, flash_message: str = ""
) -> str:
    status = controller.status()
    host_name = request_host.split(":", 1)[0] if request_host else controller.host
    if host_name in {"0.0.0.0", ""}:
        host_name = "127.0.0.1"

    webui_url = f"http://{host_name}:6185"
    log_preview = tail_text(controller.astrbot_log_file)

    def badge(ok: bool, label: str) -> str:
        css_class = "ok" if ok else "bad"
        text = "running" if ok else "stopped"
        if label.endswith("ready"):
            text = "ready" if ok else "not ready"
        return f'<span class="badge {css_class}">{label}: {text}</span>'

    message_html = ""
    if flash_message:
        message_html = f'<div class="flash">{html.escape(flash_message)}</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AstrBot Stack Control</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f4ef;
      --card: #fffdf7;
      --fg: #1d2a23;
      --muted: #65746b;
      --accent: #0f7a5a;
      --danger: #b03f2f;
      --line: #d8d3c3;
    }}
    body {{
      margin: 0;
      font-family: "Noto Sans SC", "Microsoft YaHei", sans-serif;
      background: radial-gradient(circle at top, #fff7de 0%, var(--bg) 50%, #ebe7de 100%);
      color: var(--fg);
    }}
    main {{
      max-width: 900px;
      margin: 32px auto;
      padding: 0 16px 48px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 20px;
      box-shadow: 0 14px 40px rgba(47, 58, 49, 0.08);
      margin-bottom: 16px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
    }}
    p {{
      margin: 8px 0;
      color: var(--muted);
      line-height: 1.5;
    }}
    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .badge {{
      display: inline-block;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 14px;
      font-weight: 600;
    }}
    .badge.ok {{
      background: rgba(15, 122, 90, 0.12);
      color: var(--accent);
    }}
    .badge.bad {{
      background: rgba(176, 63, 47, 0.12);
      color: var(--danger);
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 18px;
    }}
    button, .link-button {{
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    .start {{
      background: var(--accent);
      color: white;
    }}
    .restart {{
      background: #214f93;
      color: white;
    }}
    .stop {{
      background: var(--danger);
      color: white;
    }}
    .secondary {{
      background: #ece7d9;
      color: var(--fg);
    }}
    .flash {{
      background: #fff3cf;
      color: #6a5300;
      border-radius: 12px;
      padding: 12px 14px;
      margin-bottom: 16px;
      border: 1px solid #ebd48a;
      font-weight: 600;
    }}
    code, pre {{
      font-family: "JetBrains Mono", "Fira Code", monospace;
    }}
    pre {{
      margin: 0;
      padding: 14px;
      border-radius: 14px;
      background: #22251f;
      color: #ebf3e9;
      overflow-x: auto;
      font-size: 13px;
      line-height: 1.5;
    }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
      margin-top: 16px;
      color: var(--muted);
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <main>
    {message_html}
    <section class="card">
      <h1>AstrBot Stack Control</h1>
      <p>Use this page to start or stop the Doubao bridge and AstrBot together.</p>
      <div class="badges">
        {badge(bool(status["stack_running"]), "managed stack")}
        {badge(bool(status["doubao_bridge_ready"]), "doubao bridge ready")}
        {badge(bool(status["astrbot_webui_ready"]), "astrbot webui ready")}
      </div>
      <div class="actions">
        <form method="post" action="./action/start"><button class="start" type="submit">Start</button></form>
        <form method="post" action="./action/restart"><button class="restart" type="submit">Restart</button></form>
        <form method="post" action="./action/stop"><button class="stop" type="submit">Stop</button></form>
        <a class="link-button secondary" href="{html.escape(webui_url)}" target="_blank" rel="noreferrer">Open WebUI</a>
        <a class="link-button secondary" href="./status.json" target="_blank" rel="noreferrer">View JSON</a>
      </div>
      <div class="meta">
        <div>Current stack status: <code>{html.escape(str(status["codex_prod_status"]))}</code></div>
        <div>Control URL token: <code>{html.escape(str(status["control_token"]))}</code></div>
        <div>Control log: <code>{html.escape(str(status["stack_log"]))}</code></div>
        <div>WebUI: <code>{html.escape(webui_url)}</code></div>
      </div>
    </section>
    <section class="card">
      <h1>Recent AstrBot Log</h1>
      <pre>{html.escape(log_preview or "No log output yet.")}</pre>
    </section>
  </main>
</body>
</html>
"""


def make_handler(controller: StackController):
    token_prefix = f"/{controller.token}"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            if path in {token_prefix, f"{token_prefix}/"}:
                flash = urllib.parse.parse_qs(parsed.query).get("message", [""])[0]
                payload = render_page(
                    controller,
                    request_host=self.headers.get("Host", ""),
                    flash_message=flash,
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if path == f"{token_prefix}/status.json":
                payload = json.dumps(
                    controller.status(), ensure_ascii=False, indent=2
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            if path == f"{token_prefix}/action/start":
                ok, message = controller.start()
            elif path == f"{token_prefix}/action/stop":
                ok, message = controller.stop()
            elif path == f"{token_prefix}/action/restart":
                ok, message = controller.restart()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            status_code = HTTPStatus.SEE_OTHER if ok else HTTPStatus.TEMPORARY_REDIRECT
            location = f"{token_prefix}/?message={urllib.parse.quote(message)}"
            self.send_response(status_code)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            super().log_message(format, *args)

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve a small web UI to control the AstrBot foreground stack."
    )
    parser.add_argument(
        "--host", default=os.environ.get("STACK_CONTROL_HOST", "0.0.0.0")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("STACK_CONTROL_PORT", "8791"))
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path(os.environ.get("STACK_CONTROL_ROOT", DEFAULT_RUNTIME_ROOT)),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    controller = StackController(
        runtime_root=args.runtime_root,
        host=args.host,
        port=args.port,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(controller))
    print(
        f"AstrBot stack control listening on http://{args.host}:{args.port}/{controller.token}/",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
