from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent import chat as chat_mod


class _InputUI:
    def __init__(self, value: str):
        self.value = value

    def read_command(self, current_mode: str, chat_title: str | None = None) -> str:
        return self.value

    def info(self, message: str) -> None:
        pass


@pytest.mark.parametrize(
    ("raw", "mode", "expected"),
    [
        ("", "chat", {"action": "noop"}),
        ("/help", "chat", {"action": "help"}),
        ("/quit", "chat", {"action": "quit"}),
        ("/exit", "code", {"action": "exit_mode"}),
        ("/sessions", "chat", {"action": "sessions"}),
        ("/new", "chat", {"action": "new_chat"}),
        ("/continue 2", "chat", {"action": "continue_chat", "choice": "2"}),
        ("/ask explain RAG", "code", {"action": "chat", "text": "explain RAG"}),
        ("/code fix the parser", "chat", {"action": "code", "text": "fix the parser"}),
        ("/repair tests fail", "chat", {"action": "repair", "text": "tests fail"}),
        ("plain question", "chat", {"action": "chat", "text": "plain question"}),
        ("implement it", "code", {"action": "code", "text": "implement it"}),
        ("fix it", "repair", {"action": "repair", "text": "fix it"}),
    ],
)
def test_interactive_commands_route_without_running_the_agent(raw, mode, expected):
    assert chat_mod.parse_interactive_input(_InputUI(raw), mode) == expected


def test_multiline_command_routes_to_current_mode(monkeypatch):
    lines = iter(["first line", "second line", "."])
    monkeypatch.setattr("builtins.input", lambda: next(lines))

    result = chat_mod.parse_interactive_input(_InputUI("/multi"), "code")

    assert result == {"action": "code", "text": "first line\nsecond line"}


def test_direct_chat_revises_wrong_language_and_preserves_history(tmp_path: Path, monkeypatch):
    responses = [
        "This answer is entirely in English and should be revised for the Chinese user.",
        "这是经过语言修正后的回答。",
    ]
    purposes: list[str] = []

    class _FakeClient:
        def __init__(self, config_path, messages_path=None):
            pass

        def chat(self, messages, purpose):
            purposes.append(purpose)
            return responses.pop(0)

    summaries = iter(
        [
            {"totals": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            {"totals": {"calls": 2, "prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}},
        ]
    )
    monkeypatch.setattr(chat_mod, "OpenAICompatClient", _FakeClient)
    monkeypatch.setattr(chat_mod, "summarize_token_usage", lambda path: next(summaries))
    history: list[dict[str, str]] = []

    answer, usage = chat_mod.run_direct_chat(
        question="请解释一下 RAG。",
        history=history,
        messages_path=tmp_path / "messages.jsonl",
    )

    assert answer == "这是经过语言修正后的回答。"
    assert purposes == ["direct_chat", "direct_chat_language_revision"]
    assert history == [
        {"role": "user", "content": "请解释一下 RAG。"},
        {"role": "assistant", "content": "这是经过语言修正后的回答。"},
    ]
    assert usage["calls"] == 2
    assert usage["total_tokens"] == 42
