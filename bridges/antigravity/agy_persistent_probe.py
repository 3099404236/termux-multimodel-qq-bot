from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pexpect
import pyte


def visible_screen(screen: pyte.Screen) -> str:
    return "\n".join(line.rstrip() for line in screen.display if line.rstrip())


def capture_for(
    child: pexpect.spawn,
    stream: pyte.Stream,
    *,
    seconds: float,
) -> tuple[str, float]:
    started_at = time.monotonic()
    output: list[str] = []

    while child.isalive() and time.monotonic() - started_at < seconds:
        try:
            chunk = child.read_nonblocking(size=65536, timeout=0.2)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        output.append(chunk)
        stream.feed(chunk)

    return "".join(output), time.monotonic() - started_at


def run_probe(args: argparse.Namespace) -> None:
    workdir = Path(args.workdir).expanduser().resolve()
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
    }
    child = pexpect.spawn(
        args.agy_bin,
        ["--prompt-interactive", args.first_prompt, "--sandbox"],
        cwd=str(workdir),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        echo=False,
        dimensions=(args.rows, args.columns),
        timeout=1,
    )
    screen = pyte.Screen(args.columns, args.rows)
    stream = pyte.Stream(screen)

    try:
        first_output, first_seconds = capture_for(
            child,
            stream,
            seconds=args.first_capture_seconds,
        )
        print(f"first_capture_seconds={first_seconds:.3f}", flush=True)
        print("first_screen=", flush=True)
        print(visible_screen(screen), flush=True)

        child.send("\x1b[200~")
        child.send(args.second_prompt)
        child.send("\x1b[201~")
        child.send("\r")
        second_output, second_seconds = capture_for(
            child,
            stream,
            seconds=args.second_capture_seconds,
        )
        print(f"second_capture_seconds={second_seconds:.3f}", flush=True)
        print(f"process_alive={child.isalive()}", flush=True)
        print("second_screen=", flush=True)
        print(visible_screen(screen), flush=True)
        print(f"raw_character_count={len(first_output + second_output)}")
    finally:
        if child.isalive():
            child.terminate(force=True)
        child.close(force=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agy-bin", default="/usr/local/bin/agy")
    parser.add_argument(
        "--workdir",
        default="/root/antigravity-bridge/workdir",
    )
    parser.add_argument(
        "--first-prompt",
        default="Reply with exactly FIRST_RESPONSE_7Q9 and nothing else.",
    )
    parser.add_argument(
        "--second-prompt",
        default=(
            "This is a multiline second turn.\n"
            "Reply with exactly SECOND_RESPONSE_4K2 and nothing else."
        ),
    )
    parser.add_argument("--first-capture-seconds", type=float, default=24)
    parser.add_argument("--second-capture-seconds", type=float, default=24)
    parser.add_argument("--columns", type=int, default=160)
    parser.add_argument("--rows", type=int, default=40)
    return parser.parse_args()


if __name__ == "__main__":
    run_probe(parse_args())
