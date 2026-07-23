from coding_agent.contracts.analysis_contract import apply_section_coverage_review


def test_section_coverage_review_satisfies_semantic_heading_match():
    check = {
        "ok": False,
        "required_section_results": [
            {"section": "Relevant Files", "present": True},
            {"section": "Result/Summary Schema", "present": False},
        ],
        "missing_sections": ["Result/Summary Schema"],
        "failures": ["missing required task-focused sections: ['Result/Summary Schema']"],
    }
    review = {
        "section_coverage": [
            {
                "required_section": "Result/Summary Schema",
                "covered": True,
                "matched_heading": "Collected Output Format",
                "reason": "The heading describes the schema of collected result and summary outputs.",
            }
        ]
    }

    out = apply_section_coverage_review(check, review)

    assert out["ok"] is True
    assert out["missing_sections"] == []
    result = out["required_section_results"][1]
    assert result["present"] is True
    assert result["semantic_present"] is True
    assert result["matched_heading"] == "Collected Output Format"


def test_section_coverage_review_does_not_clear_unrelated_failures():
    check = {
        "ok": False,
        "required_section_results": [{"section": "Result/Summary Schema", "present": False}],
        "missing_sections": ["Result/Summary Schema"],
        "failures": [
            "missing required task-focused sections: ['Result/Summary Schema']",
            "report cites too few selected evidence paths",
        ],
    }
    review = {
        "section_coverage": [
            {
                "required_section": "Result/Summary Schema",
                "covered": True,
                "matched_heading": "Collected Output Format",
                "reason": "same meaning",
            }
        ]
    }

    out = apply_section_coverage_review(check, review)

    assert out["ok"] is False
    assert out["missing_sections"] == []
    assert out["failures"] == ["report cites too few selected evidence paths"]
