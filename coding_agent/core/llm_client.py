from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from .utils import append_jsonl, now_iso, sha16, truncate
from coding_agent.ux.token_usage import normalize_token_usage


class LLMTimeoutError(RuntimeError):
    pass


def _read_response_bytes_with_deadline(response: Any, timeout_sec: float) -> bytes:
    """Read a response body with a total deadline, not only idle timeouts.

    ``urllib`` applies its timeout to individual socket operations.  A broken
    proxy or chunked server can therefore keep a response alive indefinitely
    by sending occasional bytes.  HTTPResponse exposes ``read1``; checking a
    monotonic deadline between those bounded reads enforces the configured
    wall-clock limit.  The timer closes a currently blocked response at the
    deadline as a second line of defense.
    """
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        return response.read()

    timeout = max(0.001, float(timeout_sec))
    deadline = time.monotonic() + timeout
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        try:
            response.close()
        except Exception:
            pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    chunks: list[bytes] = []
    try:
        while True:
            if expired.is_set() or time.monotonic() >= deadline:
                raise socket.timeout("LLM response body exceeded total deadline")
            try:
                chunk = read1(65536)
            except Exception as exc:
                if expired.is_set() or time.monotonic() >= deadline:
                    raise socket.timeout("LLM response body exceeded total deadline") from exc
                raise
            if expired.is_set() or time.monotonic() >= deadline:
                raise socket.timeout("LLM response body exceeded total deadline")
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        timer.cancel()


MESSAGE_STREAM_CHUNK_EVENT_MARKERS = (
    "stream_chunk",
    "stream_delta",
    "message_chunk",
    "llm_delta",
)


def _is_message_stream_chunk_event(event_type: str) -> bool:
    low = str(event_type or "").lower()
    return any(marker in low for marker in MESSAGE_STREAM_CHUNK_EVENT_MARKERS)


def _message_kind(event_type: str) -> str:
    if event_type == "llm_request":
        return "llm_call"
    if event_type == "llm_response":
        return "llm_result"
    if event_type in {"llm_error", "llm_timeout"}:
        return "llm_failure"
    if event_type.startswith("llm_model_"):
        return "llm_metadata"
    return "message_event"


def _load_yaml_like(path: str | Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line and not line.startswith("-"):
            k, v = line.split(":", 1)
            v = v.strip()
            if v == "":
                data[k.strip()] = []
            elif v.lower() in {"true", "false"}:
                data[k.strip()] = v.lower() == "true"
            else:
                try:
                    data[k.strip()] = float(v) if "." in v else int(v)
                except ValueError:
                    data[k.strip()] = v
    return data


def _local_config_path(path: str | Path) -> Path:
    source = Path(path)
    if source.suffix:
        return source.with_name(f"{source.stem}.local{source.suffix}")
    return source / "model.local.yaml"


def _load_llm_config(config_path: str | Path | None) -> tuple[dict[str, Any], list[str]]:
    cfg: dict[str, Any] = {}
    loaded: list[str] = []
    if config_path and Path(config_path).exists():
        path = Path(config_path)
        cfg.update(_load_yaml_like(path))
        loaded.append(str(path))
        local_path = _local_config_path(path)
        if local_path.exists():
            cfg.update(_load_yaml_like(local_path))
            loaded.append(str(local_path))

    env_config = os.getenv("AGENT_LLM_CONFIG")
    if env_config and Path(env_config).exists():
        path = Path(env_config)
        cfg.update(_load_yaml_like(path))
        loaded.append(str(path))
        local_path = _local_config_path(path)
        if local_path.exists():
            cfg.update(_load_yaml_like(local_path))
            loaded.append(str(local_path))
    return cfg, loaded


def _env_or_cfg(name: str, cfg: dict[str, Any], key: str, default: Any) -> Any:
    value = os.getenv(name)
    if value is not None:
        return value
    return cfg.get(key, default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "none", "null", ""}


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 2) // 3)


class OpenAICompatClient:
    """Minimal OpenAI-compatible chat client with timeout resilience."""

    def __init__(self, config_path: str | Path | None = None, messages_path: str | Path | None = None):
        cfg, loaded_config_paths = _load_llm_config(config_path)
        model_from_env = os.getenv("AGENT_LLM_MODEL")
        self.loaded_config_paths = loaded_config_paths
        self.base_url = str(_env_or_cfg("AGENT_LLM_BASE_URL", cfg, "base_url", "http://127.0.0.1:8000/v1")).rstrip("/")
        self.api_key = str(_env_or_cfg("AGENT_LLM_API_KEY", cfg, "api_key", "EMPTY"))
        self.model = model_from_env or str(cfg.get("model", "Qwen3-Coder-30B-A3B-Instruct"))
        self.model_from_env = bool(model_from_env)
        self.auto_model = (
            _as_bool(_env_or_cfg("AGENT_LLM_AUTO_MODEL", cfg, "auto_model", True), default=True)
            and not self.model_from_env
        )
        self._model_checked = False
        self.temperature = float(_env_or_cfg("AGENT_LLM_TEMPERATURE", cfg, "temperature", 0.1))
        self.max_tokens = int(_env_or_cfg("AGENT_LLM_MAX_TOKENS", cfg, "max_tokens", 2048))
        self.context_window_tokens = int(_env_or_cfg("AGENT_LLM_CONTEXT_WINDOW", cfg, "context_window_tokens", 16384))
        default_max_input = max(1024, self.context_window_tokens - self.max_tokens - 768)
        self.max_input_tokens = int(_env_or_cfg("AGENT_LLM_MAX_INPUT_TOKENS", cfg, "max_input_tokens", default_max_input))
        self.timeout_sec = int(_env_or_cfg("AGENT_LLM_TIMEOUT", cfg, "timeout_sec", cfg.get("timeout", 180)))
        self.retry_on_timeout = int(_env_or_cfg("AGENT_LLM_TIMEOUT_RETRIES", cfg, "retry_on_timeout", 1))
        self.thinking = str(_env_or_cfg("AGENT_LLM_THINKING", cfg, "thinking", "") or "").strip().lower()
        if self.thinking in {"0", "false", "no", "off", "none", "null"}:
            self.thinking = ""
        self.analysis_report_max_tokens = int(_env_or_cfg("AGENT_ANALYZE_REPORT_MAX_TOKENS", cfg, "analysis_report_max_tokens", max(4000, self.max_tokens)))
        self.analysis_report_context_chars = int(_env_or_cfg("AGENT_ANALYZE_REPORT_CONTEXT_CHARS", cfg, "analysis_report_context_chars", 56000))
        self.analysis_report_revisions = int(_env_or_cfg("AGENT_ANALYZE_REPORT_REVISIONS", cfg, "analysis_report_revisions", 1))
        self.messages_path = str(messages_path) if messages_path else None

    def _append_message_log(self, row: dict[str, Any]) -> None:
        if self.messages_path:
            payload = dict(row)
            event_type = str(payload.get("event_type") or payload.get("type") or "message_event")
            if _is_message_stream_chunk_event(event_type):
                return
            reserved_payload = {}
            for key in ("ts", "schema", "event", "event_type", "channel", "source", "message_kind"):
                if key in payload:
                    reserved_payload[key] = payload.pop(key)
            payload.setdefault("type", event_type)
            out = {
                "ts": now_iso(),
                "schema": "message_event_v2",
                "event": event_type,
                "event_type": event_type,
                "channel": "llm",
                "source": "llm_client",
                "message_kind": _message_kind(event_type),
                **payload,
            }
            if reserved_payload:
                out["reserved_payload"] = reserved_payload
            append_jsonl(
                self.messages_path,
                out,
            )

    def _list_server_models(self) -> list[str]:
        url = f"{self.base_url}/models"
        req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout_sec, 15)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self._append_message_log({
                "type": "llm_model_discovery_failed",
                "base_url": self.base_url,
                "error": truncate(str(e), 1000),
            })
            return []
        ids: list[str] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        return ids

    def _maybe_autoselect_model(self) -> None:
        if self._model_checked or not self.auto_model:
            return
        self._model_checked = True
        ids = self._list_server_models()
        if not ids or self.model in ids:
            self._append_message_log({
                "type": "llm_model_discovery",
                "base_url": self.base_url,
                "configured_model": self.model,
                "available_models": ids[:20],
                "changed": False,
            })
            return
        old = self.model
        self.model = ids[0]
        self._append_message_log({
            "type": "llm_model_autoselect",
            "base_url": self.base_url,
            "old_model": old,
            "new_model": self.model,
            "available_models": ids[:20],
            "reason": "configured model was not served and AGENT_LLM_MODEL was not explicitly set",
        })

    def _fit_messages(self, messages: list[dict[str, str]], max_tokens: int, *, compact_factor: float = 1.0) -> tuple[list[dict[str, str]], dict[str, Any]]:
        budget = max(1024, min(int(self.max_input_tokens * compact_factor), self.context_window_tokens - max_tokens - 768))
        serialized = json.dumps(messages, ensure_ascii=False)
        before_tokens = estimate_tokens(serialized)
        if before_tokens <= budget:
            return messages, {"trimmed": False, "input_tokens_est": before_tokens, "input_token_budget": budget, "compact_factor": compact_factor}

        fitted = [dict(m) for m in messages]
        user_indices = [i for i, m in enumerate(fitted) if m.get("role") == "user"]
        candidate_indices = user_indices or list(range(len(fitted)))
        idx = max(candidate_indices, key=lambda i: len(fitted[i].get("content", "")))
        other = [m for i, m in enumerate(fitted) if i != idx]
        other_tokens = estimate_tokens(json.dumps(other, ensure_ascii=False))
        remaining_tokens = max(512, budget - other_tokens)
        char_budget = max(1200, remaining_tokens * 3)
        original = fitted[idx].get("content", "")
        fitted[idx]["content"] = truncate(original, char_budget) + (
            "\n\n[Agent context-budget note: this message was automatically truncated before the LLM call. "
            "Use trace.jsonl/state_snapshot.json for the full raw context.]"
        )
        after_tokens = estimate_tokens(json.dumps(fitted, ensure_ascii=False))
        return fitted, {
            "trimmed": True,
            "input_tokens_est_before": before_tokens,
            "input_tokens_est_after": after_tokens,
            "input_token_budget": budget,
            "trimmed_message_index": idx,
            "original_chars": len(original),
            "kept_chars": len(fitted[idx].get("content", "")),
            "compact_factor": compact_factor,
        }

    def _request_once(
        self,
        messages: list[dict[str, str]],
        purpose: str,
        max_tokens: int,
        *,
        attempt: int,
        compact_factor: float,
        allow_model_recovery: bool = True,
    ) -> str:
        self._maybe_autoselect_model()
        url = f"{self.base_url}/chat/completions"
        fitted_messages, budget_info = self._fit_messages(messages, max_tokens, compact_factor=compact_factor)
        messages_sha = sha16(json.dumps(fitted_messages, ensure_ascii=False))
        llm_call_id = sha16(json.dumps(
            {
                "purpose": purpose,
                "attempt": attempt,
                "model": self.model,
                "max_tokens": max_tokens,
                "messages_sha16": messages_sha,
            },
            ensure_ascii=False,
            sort_keys=True,
        ))
        payload = {"model": self.model, "messages": fitted_messages, "temperature": self.temperature, "max_tokens": max_tokens}
        if self.thinking:
            payload["thinking"] = {"type": self.thinking}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        if self.messages_path:
            self._append_message_log({
                "type": "llm_request", "purpose": purpose, "attempt": attempt,
                "llm_call_id": llm_call_id,
                "model": self.model, "max_tokens": max_tokens, "timeout_sec": self.timeout_sec,
                "context_window_tokens": self.context_window_tokens, "budget_info": budget_info,
                "base_url": self.base_url,
                "loaded_config_paths": self.loaded_config_paths,
                "thinking": self.thinking or None,
                "messages_sha16": messages_sha,
                "messages_preview": truncate(json.dumps(fitted_messages, ensure_ascii=False, indent=2), 6000),
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = _read_response_bytes_with_deadline(resp, self.timeout_sec)
                data = json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 404 and self.auto_model and allow_model_recovery:
                # Some vLLM deployments serve a custom --served-model-name while
                # configs/model.yaml may be stale. If the user did not explicitly
                # set AGENT_LLM_MODEL, discover the served model and retry once.
                old = self.model
                self._model_checked = False
                ids = self._list_server_models()
                candidates = [x for x in ids if x != old]
                if candidates:
                    self.model = candidates[0]
                    self._model_checked = True
                    self._append_message_log({
                        "type": "llm_model_autoselect_after_404",
                        "purpose": purpose,
                        "old_model": old,
                        "new_model": self.model,
                        "available_models": ids[:20],
                        "detail": truncate(detail, 1000),
                    })
                    return self._request_once(
                        messages,
                        purpose,
                        max_tokens,
                        attempt=attempt,
                        compact_factor=compact_factor,
                        allow_model_recovery=False,
                    )
            if self.messages_path:
                self._append_message_log({"type": "llm_error", "purpose": purpose, "attempt": attempt, "llm_call_id": llm_call_id, "status_code": e.code, "model": self.model, "detail": truncate(detail, 4000), "budget_info": budget_info})
            raise RuntimeError(f"LLM HTTP error {e.code}: {detail}") from e
        except (TimeoutError, socket.timeout) as e:
            if self.messages_path:
                self._append_message_log({"type": "llm_timeout", "purpose": purpose, "attempt": attempt, "llm_call_id": llm_call_id, "timeout_sec": self.timeout_sec, "budget_info": budget_info})
            raise LLMTimeoutError(f"LLM request timed out after {self.timeout_sec}s for purpose={purpose}") from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                if self.messages_path:
                    self._append_message_log({"type": "llm_timeout", "purpose": purpose, "attempt": attempt, "llm_call_id": llm_call_id, "timeout_sec": self.timeout_sec, "budget_info": budget_info})
                raise LLMTimeoutError(f"LLM request timed out after {self.timeout_sec}s for purpose={purpose}") from e
            raise RuntimeError(f"LLM URL error for purpose={purpose}: {e}") from e
        text = data["choices"][0]["message"]["content"]
        if self.messages_path:
            raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            self._append_message_log({
                "type": "llm_response",
                "purpose": purpose,
                "attempt": attempt,
                "llm_call_id": llm_call_id,
                "model": self.model,
                "content_sha16": sha16(text),
                "content_preview": truncate(text, 12000),
                "raw_usage": raw_usage,
                "token_usage": normalize_token_usage(raw_usage),
            })
        return text

    def chat(self, messages: list[dict[str, str]], purpose: str, max_tokens: int | None = None) -> str:
        call_max_tokens = int(max_tokens if max_tokens is not None else self.max_tokens)
        last_err: Exception | None = None
        for attempt in range(self.retry_on_timeout + 1):
            try:
                mt = call_max_tokens if attempt == 0 else max(512, min(call_max_tokens, call_max_tokens // 2))
                compact = 1.0 if attempt == 0 else 0.55
                return self._request_once(messages, purpose, mt, attempt=attempt, compact_factor=compact)
            except LLMTimeoutError as e:
                last_err = e
                continue
        raise LLMTimeoutError(str(last_err) if last_err else f"LLM timeout for purpose={purpose}")
