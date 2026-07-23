import json
import socket
import time

from coding_agent.core import llm_client as llm_mod
from coding_agent.core.llm_client import OpenAICompatClient


LLM_ENV_KEYS = [
    "AGENT_LLM_CONFIG",
    "AGENT_LLM_BASE_URL",
    "AGENT_LLM_API_KEY",
    "AGENT_LLM_MODEL",
    "AGENT_LLM_AUTO_MODEL",
    "AGENT_LLM_TEMPERATURE",
    "AGENT_LLM_MAX_TOKENS",
    "AGENT_LLM_CONTEXT_WINDOW",
    "AGENT_LLM_MAX_INPUT_TOKENS",
    "AGENT_LLM_TIMEOUT",
    "AGENT_LLM_TIMEOUT_RETRIES",
    "AGENT_LLM_THINKING",
]


def _clear_llm_env(monkeypatch):
    for key in LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_chunked_response_has_total_wall_clock_deadline():
    class _SlowChunkedResponse:
        def __init__(self):
            self.closed = False

        def read1(self, _size):
            time.sleep(0.04)
            return b" "

        def close(self):
            self.closed = True

    response = _SlowChunkedResponse()

    try:
        llm_mod._read_response_bytes_with_deadline(response, 0.01)
    except socket.timeout:
        pass
    else:
        raise AssertionError("slow chunked response should exceed the total deadline")

    assert response.closed is True


def test_model_local_yaml_overlays_default_config(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    config = tmp_path / "model.yaml"
    local = tmp_path / "model.local.yaml"
    config.write_text(
        "\n".join(
            [
                "base_url: http://127.0.0.1:8000/v1",
                "api_key: EMPTY",
                "model: local-model",
                "auto_model: true",
                "max_tokens: 3000",
                "context_window_tokens: 32768",
            ]
        ),
        encoding="utf-8",
    )
    local.write_text(
        "\n".join(
            [
                "base_url: https://api.deepseek.com",
                "api_key: sk-test",
                "model: deepseek-v4-flash",
                "auto_model: false",
                "max_tokens: 4500",
                "context_window_tokens: 65536",
                "max_input_tokens: 60000",
                "timeout_sec: 300",
                "analysis_report_max_tokens: 7000",
                "analysis_report_context_chars: 58000",
                "analysis_report_revisions: 2",
                "thinking: disabled",
            ]
        ),
        encoding="utf-8",
    )

    client = OpenAICompatClient(config)

    assert client.loaded_config_paths == [str(config), str(local)]
    assert client.base_url == "https://api.deepseek.com"
    assert client.api_key == "sk-test"
    assert client.model == "deepseek-v4-flash"
    assert client.auto_model is False
    assert client.max_tokens == 4500
    assert client.context_window_tokens == 65536
    assert client.max_input_tokens == 60000
    assert client.timeout_sec == 300
    assert client.analysis_report_max_tokens == 7000
    assert client.analysis_report_context_chars == 58000
    assert client.analysis_report_revisions == 2
    assert client.thinking == "disabled"


def test_env_vars_override_config_file_values(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    config = tmp_path / "model.yaml"
    config.write_text(
        "\n".join(
            [
                "base_url: http://127.0.0.1:8000/v1",
                "api_key: EMPTY",
                "model: configured-model",
                "thinking: disabled",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "env-key")
    monkeypatch.setenv("AGENT_LLM_MODEL", "env-model")
    monkeypatch.setenv("AGENT_LLM_THINKING", "none")

    client = OpenAICompatClient(config)

    assert client.base_url == "https://api.example.com/v1"
    assert client.api_key == "env-key"
    assert client.model == "env-model"
    assert client.model_from_env is True
    assert client.thinking == ""


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode("utf-8")


def test_thinking_payload_is_sent_only_when_configured(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    config = tmp_path / "model.yaml"
    config.write_text(
        "\n".join(
            [
                "base_url: https://api.deepseek.com",
                "api_key: sk-test",
                "model: deepseek-v4-flash",
                "auto_model: false",
                "thinking: disabled",
            ]
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)

    client = OpenAICompatClient(config)
    assert client.chat([{"role": "user", "content": "Say OK"}], purpose="unit", max_tokens=20) == "OK"

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_thinking_payload_is_omitted_for_local_config_by_default(tmp_path, monkeypatch):
    _clear_llm_env(monkeypatch)
    config = tmp_path / "model.yaml"
    config.write_text(
        "\n".join(
            [
                "base_url: http://127.0.0.1:8000/v1",
                "api_key: EMPTY",
                "model: Qwen3-Coder-30B-A3B-Instruct",
                "auto_model: false",
            ]
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)

    client = OpenAICompatClient(config)
    assert client.chat([{"role": "user", "content": "Say OK"}], purpose="unit", max_tokens=20) == "OK"

    assert "thinking" not in captured["payload"]
