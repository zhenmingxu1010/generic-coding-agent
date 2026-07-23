from coding_agent.ux.language import contains_cjk, language_instruction_for_text, response_language_quality


def test_chinese_prompt_requests_chinese_answer_by_default():
    assert contains_cjk("帮我分析这个项目")
    instruction = language_instruction_for_text("帮我分析这个项目", artifact="final report")
    assert "Chinese" in instruction
    assert "final report" in instruction


def test_english_prompt_keeps_same_language_instruction():
    instruction = language_instruction_for_text("Analyze this project", artifact="answer")
    assert "same natural language" in instruction


def test_chinese_prompt_rejects_english_only_report():
    quality = response_language_quality(
        "帮我分析这个项目",
        "This repository is a Python project. It has models, losses, metrics, and scripts.",
        artifact="final report",
    )

    assert quality["ok"] is False
    assert quality["expected"] == "chinese"


def test_chinese_prompt_accepts_chinese_report_with_code_identifiers():
    quality = response_language_quality(
        "帮我分析这个项目",
        "这个项目主要由 src/train.py、src/models.py 和 metrics.json 组成，训练入口负责读取配置、构建模型、计算 loss，并输出评估指标。",
        artifact="final report",
    )

    assert quality["ok"] is True
