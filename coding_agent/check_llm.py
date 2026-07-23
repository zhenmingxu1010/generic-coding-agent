from __future__ import annotations

import json

from coding_agent.core.llm_client import OpenAICompatClient


def main() -> None:
    client = OpenAICompatClient("configs/model.yaml")
    models = client._list_server_models()
    print(json.dumps({
        "base_url": client.base_url,
        "configured_model": client.model,
        "model_from_env": client.model_from_env,
        "auto_model": client.auto_model,
        "available_models": models,
    }, ensure_ascii=False, indent=2))
    text = client.chat([
        {"role": "system", "content": "Return JSON only."},
        {"role": "user", "content": "Return {\"ok\": true}."},
    ], purpose="check_llm", max_tokens=32)
    print(text)


if __name__ == "__main__":
    main()
