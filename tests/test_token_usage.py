from __future__ import annotations

import json
from pathlib import Path

from coding_agent.core import llm_client as llm_mod
from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.ux.token_usage import (
    format_token_usage_markdown,
    normalize_token_usage,
    summarize_token_usage,
)


class _UsageResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens_details": {"reasoning_tokens": 5},
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 9,
                },
            }
        ).encode("utf-8")


def _write_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "base_url: https://api.deepseek.com",
                "api_key: sk-test",
                "model: deepseek-v4-flash",
                "auto_model: false",
            ]
        ),
        encoding="utf-8",
    )


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_llm_response_log_records_deepseek_token_usage(tmp_path: Path, monkeypatch):
    config = tmp_path / "model.yaml"
    messages = tmp_path / "messages.jsonl"
    _write_config(config)

    def fake_urlopen(req, timeout):
        return _UsageResponse()

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)

    client = OpenAICompatClient(config, messages_path=messages)
    assert client.chat([{"role": "user", "content": "Say OK"}], purpose="intake", max_tokens=20) == "OK"

    response = next(row for row in _jsonl(messages) if row["event"] == "llm_response")
    assert response["raw_usage"]["completion_tokens_details"]["reasoning_tokens"] == 5
    assert response["token_usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "reasoning_tokens": 5,
        "cached_tokens": 3,
        "prompt_cache_hit_tokens": 2,
        "prompt_cache_miss_tokens": 9,
        "total_tokens": 18,
    }


def test_token_usage_summary_groups_calls_by_purpose(tmp_path: Path):
    messages = tmp_path / "messages.jsonl"
    rows = [
        {
            "event": "llm_response",
            "purpose": "intake",
            "attempt": 0,
            "model": "deepseek-v4-flash",
            "llm_call_id": "a",
            "raw_usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
        {
            "event": "llm_response",
            "purpose": "repair",
            "attempt": 0,
            "model": "deepseek-v4-flash",
            "llm_call_id": "b",
            "token_usage": normalize_token_usage(
                {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 4},
                    "total_tokens": 25,
                }
            ),
        },
    ]
    messages.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    summary = summarize_token_usage(messages)

    assert summary["available"] is True
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["prompt_tokens"] == 30
    assert summary["totals"]["completion_tokens"] == 7
    assert summary["totals"]["reasoning_tokens"] == 4
    assert summary["totals"]["total_tokens"] == 37
    assert summary["by_purpose"]["intake"]["total_tokens"] == 12
    assert summary["by_purpose"]["repair"]["total_tokens"] == 25

    markdown = format_token_usage_markdown(summary)
    assert "| TOTAL | 2 | 30 | 7 | 4 | 37 |" in markdown
    assert "| intake | 1 | 10 | 2 | 0 | 12 |" in markdown
    assert "| repair | 1 | 20 | 5 | 4 | 25 |" in markdown
