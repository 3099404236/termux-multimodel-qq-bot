import importlib.util
import sys
from pathlib import Path


def _load_group_memory_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "astrbot"
        / "builtin_stars"
        / "astrbot"
        / "group_memory.py"
    )
    module_name = "astrbot_group_memory_test"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load group memory module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


GROUP_MEMORY = _load_group_memory_module()
GroupMemoryStore = GROUP_MEMORY.GroupMemoryStore
build_summary_prompt = GROUP_MEMORY.build_summary_prompt


def test_group_memory_summarizes_renders_and_searches(tmp_path):
    store = GroupMemoryStore(tmp_path / "group-memory.db")
    umo = "aiocqhttp:group:test-group"

    first_id = store.record_message(
        umo=umo,
        role="user",
        sender_id="user-a",
        sender_name="Alice",
        content="Alice 提议用模型路由处理专业问题。",
    )
    second_id = store.record_message(
        umo=umo,
        role="user",
        sender_id="user-b",
        sender_name="Bob",
        content="Bob 同意，并要求保留不同模型的独立会话。",
    )

    batch = store.next_summary_batch(umo, batch_size=2)
    assert batch is not None
    assert batch.start_message_id == first_id
    assert batch.end_message_id == second_id
    assert "facts_and_decisions" in build_summary_prompt(batch)

    summary = '{"topics":["模型路由"],"facts_and_decisions":["保留独立会话"]}'
    store.save_summary(umo, batch, summary)
    store.record_message(
        umo=umo,
        role="user",
        sender_id="user-c",
        sender_name="Carol",
        content="Carol 询问如何验证路由结果。",
    )

    context = store.render_context(umo, recent_limit=18, summary_limit=3)
    assert "模型路由" in context
    assert "Carol 询问如何验证路由结果" in context

    result = store.search(umo, query="模型路由", limit=5, hours=0)
    assert "匹配的事件摘要" in result
    assert "保留独立会话" in result


def test_group_memory_waits_for_a_complete_batch(tmp_path):
    store = GroupMemoryStore(tmp_path / "group-memory.db")
    store.record_message(
        umo="group",
        role="user",
        sender_id="user",
        sender_name="User",
        content="only one message",
    )

    assert store.next_summary_batch("group", batch_size=2) is None
