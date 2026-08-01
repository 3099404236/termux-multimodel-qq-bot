from astrbot.core.tools.doubao_web_bridge import (
    ChatSnapshot,
    _capture_incident,
    _looks_like_home_screen,
    derive_response_text,
)


def test_derive_response_text_uses_appended_body_suffix():
    before = ChatSnapshot(
        body_text="用户\n136 caf怎么1400加了",
        blocks=("用户", "136 caf怎么1400加了"),
    )
    after = ChatSnapshot(
        body_text="用户\n136 caf怎么1400加了\n这我不确定，得先看你说的是啥版本。",
        blocks=(
            "用户",
            "136 caf怎么1400加了",
            "这我不确定，得先看你说的是啥版本。",
        ),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="136 caf怎么1400加了")
        == "这我不确定，得先看你说的是啥版本。"
    )


def test_derive_response_text_filters_prompt_echo_and_ui_noise():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text="用户\n136 caf怎么1400加了\n发送\n这得看具体型号。",
        blocks=("用户", "136 caf怎么1400加了", "发送", "这得看具体型号。"),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="136 caf怎么1400加了")
        == "这得看具体型号。"
    )


def test_derive_response_text_ignores_loading_state():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text="用户\n136 caf怎么1400加了\n思考中",
        blocks=("用户", "136 caf怎么1400加了", "思考中"),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="136 caf怎么1400加了")
        == ""
    )


def test_derive_response_text_ignores_retrieval_status_only_snapshot():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text="用户\nevolpromopt是什么\n找到 12 篇资料",
        blocks=("用户", "evolpromopt是什么", "找到 12 篇资料"),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="evolpromopt是什么")
        == ""
    )


def test_derive_response_text_strips_retrieval_status_before_real_answer():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text=(
            "用户\nevolpromopt是什么\n找到 12 篇资料\n"
            "EvolPrompt 是清华团队关于提示词自我演化优化的一类工作。"
        ),
        blocks=(
            "用户",
            "evolpromopt是什么",
            "找到 12 篇资料",
            "EvolPrompt 是清华团队关于提示词自我演化优化的一类工作。",
        ),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="evolpromopt是什么")
        == "EvolPrompt 是清华团队关于提示词自我演化优化的一类工作。"
    )


def test_derive_response_text_strips_trailing_follow_up_suggestions():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text=(
            "用户\n这得看具体型号。\n你说的是哪个配置？\n在哪里买的？\n"
            "要不要我帮你一起算一下？"
        ),
        blocks=(
            "用户",
            "这得看具体型号。",
            "你说的是哪个配置？",
            "在哪里买的？",
            "要不要我帮你一起算一下？",
        ),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="136 caf怎么1400加了")
        == "这得看具体型号。"
    )


def test_derive_response_text_keeps_clarifying_questions_as_answer():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text="用户\n你指的是哪种串号？\n是设备串号还是账号关联？",
        blocks=("用户", "你指的是哪种串号？", "是设备串号还是账号关联？"),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="什么是串号")
        == "你指的是哪种串号？\n是设备串号还是账号关联？"
    )


def test_derive_response_text_strips_repeated_answer_bundle_and_video_entry():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text=(
            "用户\n挺好的，你呢？\n你能做些什么？\n你是谁？\n你是怎样运行的？\n"
            "挺好的，你呢？\n你能做些什么？\n你是谁？\n你是怎样运行的？\n"
            "你能做些什么？\n你是谁？\n你是怎样运行的？\n视频生成\n视频生成\n视频生成"
        ),
        blocks=(
            "用户",
            "挺好的，你呢？\n你能做些什么？\n你是谁？\n你是怎样运行的？\n"
            "挺好的，你呢？\n你能做些什么？\n你是谁？\n你是怎样运行的？\n"
            "你能做些什么？\n你是谁？\n你是怎样运行的？\n视频生成\n视频生成\n视频生成",
        ),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="你好吗")
        == "挺好的，你呢？"
    )


def test_derive_response_text_strips_embedded_packed_prompt_after_answer():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    prompt = "[system]\n你是助手\n\n[user]\n你今天开心吗？"
    after = ChatSnapshot(
        body_text=f"用户\n挺开心的呀\n{prompt}",
        blocks=("用户", f"挺开心的呀\n{prompt}"),
    )

    assert (
        derive_response_text(before=before, after=after, prompt=prompt)
        == "挺开心的呀"
    )


def test_derive_response_text_drops_pure_packed_prompt_leak():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    prompt = "[system]\n你是助手\n\n[user]\n你今天开心吗？"
    after = ChatSnapshot(
        body_text=f"用户\n{prompt}",
        blocks=("用户", prompt),
    )

    assert derive_response_text(before=before, after=after, prompt=prompt) == ""


def test_derive_response_text_strips_reference_badges_and_exact_repeat():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text="用户\n今天是星期六。\n参考 9 篇资料\n今天是星期六。\n参考 9 篇资料",
        blocks=(
            "用户",
            "今天是星期六。\n参考 9 篇资料\n今天是星期六。\n参考 9 篇资料",
        ),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="你知道今天星期几吗？")
        == "今天是星期六。"
    )


def test_derive_response_text_strips_weekend_suggestion_bundle():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text=(
            "用户\n周六啦🌙\n有什么适合周六做的事情？\n推荐一些适合周六放松的活动。\n"
            "周六适合和朋友一起做什么？\n周六啦🌙\n有什么适合周六做的事情？\n"
            "推荐一些适合周六放松的活动。\n周六适合和朋友一起做什么？\n"
            "有什么适合周六做的事情？\n推荐一些适合周六放松的活动。\n"
            "周六适合和朋友一起做什么？"
        ),
        blocks=(
            "用户",
            "周六啦🌙\n有什么适合周六做的事情？\n推荐一些适合周六放松的活动。\n"
            "周六适合和朋友一起做什么？\n周六啦🌙\n有什么适合周六做的事情？\n"
            "推荐一些适合周六放松的活动。\n周六适合和朋友一起做什么？\n"
            "有什么适合周六做的事情？\n推荐一些适合周六放松的活动。\n"
            "周六适合和朋友一起做什么？",
        ),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="今天星期几。")
        == "周六啦🌙"
    )


def test_derive_response_text_strips_ai_disclaimer_and_rewrite_suggestions():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text=(
            "用户\n哈哈，假装没看见就对了😶\n本回答由AI生成，仅供参考，请仔细甄别，如有需求请咨询专业人士。\n"
            "换个幽默的说法回复\n来个搞笑的段子\n分享一个有趣的冷笑话"
        ),
        blocks=(
            "用户",
            "哈哈，假装没看见就对了😶\n本回答由AI生成，仅供参考，请仔细甄别，如有需求请咨询专业人士。\n"
            "换个幽默的说法回复\n来个搞笑的段子\n分享一个有趣的冷笑话",
        ),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="/你看你回复了，说明你是人机")
        == "哈哈，假装没看见就对了😶"
    )


def test_derive_response_text_drops_short_title_chip_answer():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text="用户\n回答问题",
        blocks=("用户", "回答问题"),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="回答一下大家的问题。")
        == ""
    )


def test_derive_response_text_drops_short_title_chip_topic():
    before = ChatSnapshot(body_text="用户", blocks=("用户",))
    after = ChatSnapshot(
        body_text="用户\n科普烂梗",
        blocks=("用户", "科普烂梗"),
    )

    assert (
        derive_response_text(before=before, after=after, prompt="科普一下最新的烂梗")
        == ""
    )


def test_looks_like_home_screen_detects_intro_ui():
    assert _looks_like_home_screen(
        "\n".join(
            [
                "豆包",
                "新对话",
                "内容由豆包 AI 生成",
                "下载豆包电脑版",
                "你好，我是豆包",
                "Generate Image",
                "Translate",
            ]
        )
    )


def test_capture_incident_writes_prompt_and_metadata(tmp_path):
    incident_dir = tmp_path / "incidents"

    _capture_incident(
        incident_dir,
        prompt="[user]\n最近有什么新闻",
        requested_model="doubao-web",
        message_count=2,
        status_code=502,
        code="doubao_captcha_required",
        error_message="captcha hit",
        details={"captcha_detected": True, "page_url": "https://www.doubao.com/chat/1"},
    )

    prompt_files = sorted(incident_dir.glob("*.prompt.txt"))
    metadata_files = sorted(incident_dir.glob("*.json"))

    assert len(prompt_files) == 1
    assert len(metadata_files) == 1
    assert prompt_files[0].read_text(encoding="utf-8") == "[user]\n最近有什么新闻"

    metadata = metadata_files[0].read_text(encoding="utf-8")
    assert '"code": "doubao_captcha_required"' in metadata
    assert '"status_code": 502' in metadata
    assert '"captcha_detected": true' in metadata
