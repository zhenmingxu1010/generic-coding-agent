from __future__ import annotations

import argparse
import shutil
import traceback
from pathlib import Path

from coding_agent.graph import build_graph
from coding_agent.core.utils import read_json, write_json
from coding_agent.core.resume import prepare_resumed_state
from coding_agent.workspace.run_paths import agent_test_root_rel, run_dir_for


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LangGraph-based general coding agent")
    p.add_argument("--workspace", required=True, help="Workspace directory. Use ./path, not /tmp by default.")
    group = p.add_mutually_exclusive_group(required=False)
    group.add_argument("--task", help="Task text")
    group.add_argument("--task-file", help="Path to a task text file")
    p.add_argument("--repair-existing", action="store_true", help="Verify and repair an existing workspace instead of generating a new project from scratch")
    p.add_argument("--resume", action="store_true", help="Resume from the agent-owned .agent_runs state snapshot if it exists")
    clarification = p.add_mutually_exclusive_group(required=False)
    clarification.add_argument("--clarification-answer", help="Answer the pending clarification question while resuming.")
    clarification.add_argument("--clarification-file", help="Read the clarification answer from a UTF-8 text file while resuming.")
    p.add_argument("--thread-id", default="default", help="LangGraph checkpoint thread id")
    p.add_argument("--max-rounds", type=int, default=12)
    p.add_argument(
        "--max-repair-calls",
        type=int,
        default=6,
        help="Maximum expensive repair-node LLM decisions before a controlled stop.",
    )
    p.add_argument("--clean-agent-state", action="store_true", help="Delete this thread's .agent_runs record and .coding_agent_test files; does not delete workspace code")
    return p.parse_args()


def load_task(args: argparse.Namespace) -> str:
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8")
    if args.task:
        return args.task
    if args.repair_existing:
        return "Verify this existing workspace, diagnose failures, and repair the code without weakening tests."
    raise SystemExit("Either --task/--task-file or --repair-existing is required")


def main() -> None:
    args = parse_args()
    if (args.clarification_answer or args.clarification_file) and not args.resume:
        raise SystemExit("--clarification-answer/--clarification-file requires --resume")
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    run_dir = run_dir_for(workspace, args.thread_id)
    if args.clean_agent_state and run_dir.exists():
        shutil.rmtree(run_dir)
    agent_test_dir = workspace / agent_test_root_rel(args.thread_id)
    if args.clean_agent_state and agent_test_dir.exists():
        shutil.rmtree(agent_test_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = run_dir / "state_snapshot.json"

    if args.resume and snapshot_path.exists():
        clarification_answer = args.clarification_answer
        if args.clarification_file:
            clarification_answer = Path(args.clarification_file).read_text(encoding="utf-8")
        state = prepare_resumed_state(
            read_json(snapshot_path),
            max_rounds=args.max_rounds,
            max_repair_calls=args.max_repair_calls,
            clarification_answer=clarification_answer,
        )
    else:
        task = load_task(args)
        state = {
            "task": task,
            "user_task": task,
            "original_task": task,
            "workspace": str(workspace),
            "thread_id": args.thread_id,
            "max_rounds": args.max_rounds,
            "max_repair_llm_calls": args.max_repair_calls,
            "mode": "repair_existing" if args.repair_existing else "auto",
            "invariants": [
                "Do not use /tmp as the default output path.",
                "Do not weaken tests to hide implementation bugs; however, agent-generated tests may be corrected when a TestOracleReview finds they contradict the task contract or their own expected-value calculation.",
                "The final success decision must come from execution-based verification, not from the LLM.",
                "Every file modification must go through the agent's guarded tools.",
                "Prefer reading relevant files before writing changes.",
            ],
        }

    # Make crash recovery possible even if the graph fails before intake/common
    # initializes run artifacts. Nodes may overwrite these with the same values.
    state.setdefault("run_dir", str(run_dir))
    state.setdefault("trace_path", str(run_dir / "trace.jsonl"))
    state.setdefault("messages_path", str(run_dir / "messages.jsonl"))
    state.setdefault("context_pack_path", str(run_dir / "context_pack.json"))
    state.setdefault("context_summary_path", str(run_dir / "context_summary.md"))
    state.setdefault("state_snapshot_path", str(run_dir / "state_snapshot.json"))
    state.setdefault("patches_dir", str(run_dir / "patches"))
    state.setdefault("final_path", str(run_dir / "final.json"))

    graph = build_graph()
    try:
        result = graph.invoke(state, config={"configurable": {"thread_id": args.thread_id}})
    except Exception as e:
        tb = traceback.format_exc()
        crash_state = dict(state)
        if snapshot_path.exists():
            try:
                previous = read_json(snapshot_path)
                if isinstance(previous, dict):
                    crash_state.update(previous)
            except Exception:
                pass
        crash_state.update({
            "final_ok": False,
            "runtime_ok": False,
            "stopped_reason": "runtime_exception",
            "failure": {
                "failure_type": "runtime_exception",
                "message": str(e),
                "signature": e.__class__.__name__,
                "raw_excerpt": tb[-8000:],
            },
        })
        final_path = crash_state.get("final_path") or str(run_dir / "final.json")
        write_json(final_path, {
            "ok": False,
            "runtime_ok": False,
            "stopped_reason": "runtime_exception",
            "task": crash_state.get("task"),
            "workspace": str(workspace),
            "thread_id": args.thread_id,
            "mode": crash_state.get("mode"),
            "read_only": crash_state.get("read_only"),
            "write_locked": crash_state.get("write_locked"),
            "read_only_policy": crash_state.get("read_only_policy"),
            "failure": crash_state.get("failure"),
            "traceback_tail": tb[-8000:],
            "artifacts": {
                "trace": crash_state.get("trace_path"),
                "messages": crash_state.get("messages_path"),
                "state_snapshot": crash_state.get("state_snapshot_path"),
            },
        })
        write_json(crash_state.get("state_snapshot_path") or str(run_dir / "state_snapshot.json"), crash_state)
        print({
            "ok": False,
            "stopped_reason": "runtime_exception",
            "workspace": str(workspace),
            "final_json": final_path,
            "error": str(e),
        })
        raise SystemExit(1)
    final_path = result.get("final_path") or str(run_dir / "final.json")
    print({
        "ok": result.get("final_ok"),
        "stopped_reason": result.get("stopped_reason"),
        "round_idx": result.get("round_idx"),
        "workspace": str(workspace),
        "final_json": final_path,
        "trace": result.get("trace_path"),
        "messages": result.get("messages_path"),
        "clarification_questions": result.get("clarification_questions") or [],
    })


if __name__ == "__main__":
    main()
