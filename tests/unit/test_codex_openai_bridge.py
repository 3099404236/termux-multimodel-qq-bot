import importlib.util
import sys
from pathlib import Path


def _load_bridge_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "astrbot"
        / "core"
        / "tools"
        / "codex_openai_bridge.py"
    )
    module_name = "astrbot_codex_openai_bridge_test"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load bridge module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


BRIDGE = _load_bridge_module()
BridgeConfig = BRIDGE.BridgeConfig
build_chat_completion_response = BRIDGE.build_chat_completion_response
build_codex_command = BRIDGE.build_codex_command
build_codex_prompt = BRIDGE.build_codex_prompt
build_stream_chunk_payloads = BRIDGE.build_stream_chunk_payloads
render_message_content = BRIDGE.render_message_content


def _make_config() -> BridgeConfig:
    return BridgeConfig(
        host="127.0.0.1",
        port=8787,
        default_model="gpt-5.4",
        advertised_models=("gpt-5.4",),
        codex_bin="codex",
        workdir=Path("/tmp/workdir"),
        sandbox="read-only",
        timeout_seconds=300,
        auth_token=None,
        stream_chunk_chars=32,
        max_parallel_requests=1,
        enable_search=False,
        codex_configs=(),
    )


def test_render_message_content_handles_mixed_parts():
    rendered = render_message_content(
        [
            {"type": "text", "text": "Look at this"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        ]
    )

    assert "Look at this" in rendered
    assert "[Image omitted: https://example.com/cat.png]" in rendered


def test_build_codex_prompt_mentions_tool_limitation():
    prompt = build_codex_prompt(
        [{"role": "user", "content": "Use the weather tool."}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "weather_lookup",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice="required",
    )

    assert "weather_lookup" in prompt
    assert "cannot execute tools" in prompt
    assert "plain conversation" in prompt
    assert "Do not use shell commands" in prompt
    assert "[user]" in prompt


def test_build_chat_completion_response_matches_openai_shape():
    payload = build_chat_completion_response(
        content="Hello from Codex",
        model="gpt-5.4",
        completion_id="chatcmpl-test",
        created=123,
    )

    assert payload["id"] == "chatcmpl-test"
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert payload["choices"][0]["message"]["content"] == "Hello from Codex"
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_build_stream_chunk_payloads_emits_role_content_and_stop():
    payloads = build_stream_chunk_payloads(
        content="abcdef",
        model="gpt-5.4",
        completion_id="chatcmpl-stream",
        created=456,
        chunk_size=2,
    )

    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert payloads[1]["choices"][0]["delta"] == {"content": "ab"}
    assert payloads[2]["choices"][0]["delta"] == {"content": "cd"}
    assert payloads[3]["choices"][0]["delta"] == {"content": "ef"}
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_build_codex_command_uses_stdin_and_output_file():
    config = _make_config()
    command = build_codex_command(
        config=config,
        requested_model="gpt-5.4",
        output_path=Path("/tmp/reply.txt"),
    )

    assert command[:3] == ["codex", "exec", "--skip-git-repo-check"]
    assert "--model" in command
    assert str(Path("/tmp/reply.txt")) in command
    assert command[-1] == "-"
