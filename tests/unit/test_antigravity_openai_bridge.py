import asyncio
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path


def _load_bridge_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "astrbot"
        / "core"
        / "tools"
        / "antigravity_openai_bridge.py"
    )
    module_name = "astrbot_antigravity_openai_bridge_test"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load bridge module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


BRIDGE = _load_bridge_module()
BridgeConfig = BRIDGE.BridgeConfig
build_antigravity_command = BRIDGE.build_antigravity_command
build_antigravity_prompt = BRIDGE.build_antigravity_prompt
parse_args = BRIDGE.parse_args


def _make_config():
    return BridgeConfig(
        host="127.0.0.1",
        port=8791,
        default_model="antigravity",
        advertised_models=("antigravity",),
        agy_bin="agy",
        agy_model="gemini-3.6-flash",
        opus_model="claude-opus-4-6-thinking",
        effort="high",
        workdir=Path("/tmp/workdir"),
        timeout_seconds=180,
        print_timeout_seconds=150,
        auth_token=None,
        stream_chunk_chars=64,
        max_parallel_requests=1,
        sandbox=True,
        reuse_conversations=False,
        session_max_turns=20,
        session_idle_seconds=21600,
        session_state_file=Path("/tmp/session-state.json"),
        persistent_tui=False,
        persistent_session_limit=2,
        persistent_columns=160,
        persistent_rows=50,
        persistent_write_timeout_seconds=5,
        persistent_submit_timeout_seconds=20,
        persistent_progress_timeout_seconds=180,
        persistent_buffer_screens=256,
    )


def test_build_antigravity_prompt_preserves_roles_and_describes_tools():
    prompt = build_antigravity_prompt(
        [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Check the weather."},
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "weather_lookup"},
            }
        ],
        tool_choice="required",
    )

    assert "[system]" in prompt
    assert "[user]" in prompt
    assert "weather_lookup" in prompt
    assert "request exactly one appropriate function" in prompt
    assert "Do not run local shell commands" in prompt
    assert "strict YAML envelope" in prompt


def test_model_routing_requires_an_explicit_latest_user_request():
    config = _make_config()

    assert (
        BRIDGE._select_model_route(
            [{"role": "user", "content": "请使用 Opus 分析这个问题"}],
            config=config,
            session_key="group-1",
        )[0]
        == "opus"
    )
    assert (
        BRIDGE._select_model_route(
            [{"role": "user", "content": "请用 GPT 回答"}],
            config=config,
            session_key="group-1",
        )[0]
        == "gpt"
    )
    assert (
        BRIDGE._select_model_route(
            [{"role": "user", "content": "不要用 GPT，直接回答"}],
            config=config,
            session_key="group-1",
        )[0]
        == "default"
    )
    assert (
        BRIDGE._select_model_route(
            [{"role": "user", "content": "请用 Opus 压缩"}],
            config=config,
            session_key="group-memory-compressor:group-1",
        )[0]
        == "default"
    )


def test_build_antigravity_command_uses_print_mode_and_sandbox():
    command = build_antigravity_command(
        config=_make_config(),
        prompt="Reply with hello.",
    )

    assert command[:3] == ["agy", "-p", "Reply with hello."]
    assert command[command.index("--output-format") + 1] == "text"
    assert command[command.index("--print-timeout") + 1] == "150s"
    assert "--sandbox" in command
    assert command[command.index("--model") + 1] == "gemini-3.6-flash"
    assert command[command.index("--effort") + 1] == "high"


def test_parse_args_advertises_unique_models_and_can_disable_sandbox(tmp_path):
    config = parse_args(
        [
            "--model",
            "antigravity",
            "--advertise-model",
            "gemini",
            "--advertise-model",
            "antigravity",
            "--disable-sandbox",
            "--workdir",
            str(tmp_path),
        ]
    )

    assert config.advertised_models == ("antigravity", "gemini")
    assert config.workdir == tmp_path.resolve()
    assert config.sandbox is False


def test_parse_args_enables_manager_backend(tmp_path):
    config = parse_args(
        [
            "--manager-base-url",
            "http://manager.example:8045/v1",
            "--manager-api-key",
            "test-secret",
            "--manager-model",
            "gemini-test-model",
            "--manager-timeout",
            "45",
            "--workdir",
            str(tmp_path),
        ]
    )

    assert config.manager_base_url == "http://manager.example:8045/v1"
    assert config.manager_api_key == "test-secret"
    assert config.manager_model == "gemini-test-model"
    assert config.manager_timeout_seconds == 45


def test_manager_backend_forwards_openai_messages_and_tools(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_weather",
                                        "type": "function",
                                        "function": {
                                            "name": "weather_lookup",
                                            "arguments": '{"city":"Shanghai"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(BRIDGE.urllib.request, "urlopen", fake_urlopen)
    config = replace(
        _make_config(),
        manager_base_url="http://manager.example:8045/v1",
        manager_api_key="test-secret",
        manager_model="gemini-test-model",
        manager_timeout_seconds=45,
    )
    messages = [{"role": "user", "content": "Check Shanghai weather."}]
    tools = [
        {
            "type": "function",
            "function": {"name": "weather_lookup", "parameters": {}},
        }
    ]

    result = asyncio.run(
        BRIDGE.run_antigravity_prompt(
            prompt="unused manager prompt",
            requested_model="antigravity",
            config=config,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
    )

    assert captured["url"] == ("http://manager.example:8045/v1/chat/completions")
    assert captured["authorization"] == "Bearer test-secret"
    assert captured["timeout"] == 45
    assert captured["payload"]["model"] == "gemini-test-model"
    assert captured["payload"]["messages"][1:] == messages
    assert "explicitly asks to use Exa" in captured["payload"]["messages"][0]["content"]
    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == "auto"
    assert result.content is None
    assert result.tool_calls[0]["function"]["name"] == "weather_lookup"


def test_openai_compatible_chat_endpoint(monkeypatch):
    async def fake_runner(*, prompt, requested_model, config, **_):
        assert "[user]\nHello" in prompt
        assert requested_model == "antigravity"
        assert config.default_model == "antigravity"
        return "Hello from Antigravity"

    monkeypatch.setattr(BRIDGE, "run_antigravity_prompt", fake_runner)
    app = BRIDGE.create_app(_make_config())

    async def request_completion():
        response = await app.test_client().post(
            "/v1/chat/completions",
            json={
                "model": "antigravity",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        return response.status_code, await response.get_json()

    status_code, payload = asyncio.run(request_completion())

    assert status_code == 200
    assert payload["model"] == "antigravity"
    assert payload["choices"][0]["message"]["content"] == ("Hello from Antigravity")
