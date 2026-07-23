from __future__ import annotations

from coding_agent.verification.test_baseline import compare_with_test_baseline


def _failure(test: str, message: str, kind: str = "AssertionError") -> dict:
    return {"test": test, "type": kind, "message": message, "owner": "implementation"}


def _baseline(*failures: dict, total: int = 4) -> dict:
    return {
        "version": "test_baseline_v1",
        "captured": True,
        "comparable": True,
        "total": total,
        "failures": list(failures),
    }


def _current(*failures: dict, total: int = 4) -> dict:
    return {
        "version": "run_tests_v1",
        "ok": False,
        "timed_out": False,
        "total": total,
        "failures": list(failures),
    }


def test_accepts_identical_preexisting_failure() -> None:
    failure = _failure("tests.test_api::test_old", "KeyError: 'old'")
    result = compare_with_test_baseline(_baseline(failure), _current(failure))
    assert result["accepted_preexisting_failures"] is True


def test_accepts_subset_when_other_baseline_failure_was_fixed() -> None:
    remaining = _failure("tests.test_api::test_old", "KeyError: 'old'")
    fixed = _failure("tests.test_api::test_other", "ValueError: old")
    result = compare_with_test_baseline(
        _baseline(remaining, fixed),
        _current(remaining),
    )
    assert result["accepted_preexisting_failures"] is True


def test_rejects_new_failure_even_when_test_name_is_old() -> None:
    before = _failure("tests.test_api::test_old", "KeyError: 'old'")
    after = _failure("tests.test_api::test_old", "KeyError: 'new'")
    result = compare_with_test_baseline(_baseline(before), _current(after))
    assert result["accepted_preexisting_failures"] is False
    assert result["reason"] == "new_test_failures"


def test_rejects_reduced_test_collection() -> None:
    failure = _failure("tests.test_api::test_old", "KeyError: 'old'")
    result = compare_with_test_baseline(
        _baseline(failure, total=4),
        _current(failure, total=3),
    )
    assert result["accepted_preexisting_failures"] is False
    assert result["reason"] == "current_run_timed_out_or_collected_fewer_tests"


def test_rejects_non_comparable_baseline() -> None:
    failure = _failure("tests.test_api::test_old", "KeyError: 'old'")
    baseline = _baseline(failure)
    baseline["comparable"] = False
    result = compare_with_test_baseline(baseline, _current(failure))
    assert result["accepted_preexisting_failures"] is False
    assert result["reason"] == "baseline_not_comparable"
