from pathlib import Path

from coding_agent.ux.chat_store import (
    append_chat_turn,
    chat_session_paths,
    create_chat_session,
    list_chat_sessions,
    load_chat_history,
    resolve_chat_session_choice,
    title_from_text,
)


def test_chat_store_saves_lists_and_restores_history(tmp_path: Path):
    meta = create_chat_session(
        tmp_path,
        workspace="/workspace/demo",
        model="demo-model",
        base_url="http://llm.local/v1",
    )

    append_chat_turn(tmp_path, meta["id"], user_text="这是第一个问题，需要生成标题", assistant_text="回答")

    sessions = list_chat_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0]["id"] == meta["id"]
    assert sessions[0]["title"].startswith("这是第一个问题")
    assert sessions[0]["turns"] == 1

    history = load_chat_history(tmp_path, meta["id"])
    assert history == [
        {"role": "user", "content": "这是第一个问题，需要生成标题"},
        {"role": "assistant", "content": "回答"},
    ]

    assert resolve_chat_session_choice(tmp_path, "1")["id"] == meta["id"]
    assert resolve_chat_session_choice(tmp_path, meta["id"])["id"] == meta["id"]
    assert chat_session_paths(tmp_path, meta["id"])["llm_messages"].name == "llm_messages.jsonl"


def test_title_from_text_is_short_and_stable():
    assert title_from_text("") == "新对话"
    assert title_from_text("  hello   world  ") == "hello world"
    assert len(title_from_text("x" * 100, max_chars=10)) == 10


def test_chat_session_list_hides_empty_sessions_by_default(tmp_path: Path):
    empty = create_chat_session(
        tmp_path,
        workspace="/workspace/demo",
        model="demo-model",
        base_url="http://llm.local/v1",
    )
    filled = create_chat_session(
        tmp_path,
        workspace="/workspace/demo",
        model="demo-model",
        base_url="http://llm.local/v1",
    )
    append_chat_turn(tmp_path, filled["id"], user_text="问题", assistant_text="回答")

    sessions = list_chat_sessions(tmp_path)
    assert [item["id"] for item in sessions] == [filled["id"]]
    sessions_with_empty = list_chat_sessions(tmp_path, include_empty=True)
    assert {item["id"] for item in sessions_with_empty} == {empty["id"], filled["id"]}
