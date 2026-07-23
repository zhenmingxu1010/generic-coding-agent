from coding_agent.core.llm_client import OpenAICompatClient, estimate_tokens


def test_fit_messages_trims_long_user_message(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("AGENT_LLM_CONTEXT_WINDOW", "16384")
    monkeypatch.setenv("AGENT_LLM_MAX_INPUT_TOKENS", "6000")
    client = OpenAICompatClient(messages_path=tmp_path / "messages.jsonl")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "x" * 50000},
    ]
    fitted, info = client._fit_messages(messages, max_tokens=2048)
    assert info["trimmed"] is True
    assert estimate_tokens(str(fitted)) <= info["input_token_budget"] + 200
    assert "context-budget note" in fitted[1]["content"]


def test_fit_messages_keeps_short_message(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_INPUT_TOKENS", "6000")
    client = OpenAICompatClient(messages_path=tmp_path / "messages.jsonl")
    messages = [{"role": "user", "content": "small"}]
    fitted, info = client._fit_messages(messages, max_tokens=1024)
    assert fitted == messages
    assert info["trimmed"] is False
