from coding_agent.nodes.analyze_report import _report_instructions


def test_report_instructions_prioritize_complete_required_sections():
    text = _report_instructions({"report_type": "repository_overview"})

    assert "The report must include every required top-level section before adding detail." in text
    assert "Do not spend the output budget on long code blocks or long JSON excerpts." in text
    assert "File Responsibility Table" in text
    assert "Question Coverage Checklist" in text
