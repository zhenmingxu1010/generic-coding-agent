from __future__ import annotations

import json
from pathlib import Path

from coding_agent.core.schemas import CommandResult, ToolResult, VerificationResult
from coding_agent.core.implementation_batch import update_implementation_batch
from coding_agent.tools.registry import READ_TOOLS as REGISTRY_READ_TOOLS
from coding_agent.tools.registry import WRITE_TOOLS as REGISTRY_WRITE_TOOLS
from coding_agent.tools.registry import execute_tool
from coding_agent.workspace.artifacts import build_artifact_registry, artifact_for_path
from coding_agent.scope.write_guard import prewrite_backup
from coding_agent.scope.write_intent import can_execute_write_intent
from coding_agent.memory.artifact_provenance import record_artifact_event
from coding_agent.verification.test_registry import refresh_verification_test_registry, registered_test_paths
from coding_agent.verification.test_path_policy import normalize_generated_test_write_path
from coding_agent.memory.trace_payloads import requirement_atom_trace_status
from coding_agent.repair.read_cache import append_read_chunk, cache_key, request_is_cached
from coding_agent.workspace.failed_writes import record_failed_write
from coding_agent.workspace.run_paths import is_agent_test_path, is_test_like_path, mapped_agent_test_for_original
from coding_agent.core.utils import sha16, truncate
from coding_agent.safety.path_guard import is_within_workspace
from .common import get_trace


EMPTY_SHA16 = "e3b0c44298fc1c14"
REPEAT_GUARDED_TOOLS = {"run_shell"}
EXPLORATION_TOOLS = set(REGISTRY_READ_TOOLS) | {"run_shell", "git_diff", "inspect_python", "filter_files", "search_text"}
MAX_EXPLORATION_ACTIONS_PER_FAILURE = 3


def _action_key(tool: str, args: dict) -> str:
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False, sort_keys=True, default=str)


def _empty_result(result: dict) -> bool:
    data = result.get("data", {}) or {}
    if result.get("tool") == "search_text":
        return not data.get("matches")
    if result.get("tool") == "filter_files":
        return not data.get("matches")
    if result.get("tool") == "list_files":
        return not data.get("files")
    return False


def _repeat_action_policy_result(state: dict, tool: str, args: dict, key: str) -> ToolResult | None:
    if tool not in REPEAT_GUARDED_TOOLS or not state.get("failure"):
        return None
    sig = _same_failure_key(state)
    repeat_count = 0
    for item in reversed(state.get("action_history") or []):
        if item.get("tool") in {"write_file", "edit_file"} and item.get("changed"):
            break
        if item.get("failure_signature") != sig:
            continue
        if item.get("action_key") == key:
            repeat_count += 1
    if repeat_count < 2:
        return None
    force = _set_force_repair_action(
        state,
        reason="same executable command repeated for the same unresolved failure without a file-changing repair",
        path=None,
        blocked_tool=tool,
        allowed_tools=["edit_file", "write_file", "run_tests", "finish"],
        extra={"repeated_action_key": key, "repeat_count": repeat_count + 1},
    )
    return ToolResult(
        tool=tool,
        ok=False,
        message="repeated shell command blocked; change repair strategy, run structured tests, or finish with reason",
        data={
            "blocked_by_repeated_action_guard": True,
            "action_key": key,
            "repeat_count": repeat_count + 1,
            "force_repair_action": force,
            "attempted_command": args.get("command") if isinstance(args, dict) else None,
        },
    )


def _generic_exploration_budget_result(state: dict, tool: str, args: dict) -> ToolResult | None:
    if tool not in EXPLORATION_TOOLS or not state.get("failure"):
        return None
    sig = _same_failure_key(state)
    count = 0
    for item in reversed(state.get("action_history") or []):
        if item.get("tool") in {"write_file", "edit_file"} and item.get("changed"):
            break
        if item.get("failure_signature") != sig:
            continue
        if item.get("tool") in EXPLORATION_TOOLS:
            count += 1
    if count < MAX_EXPLORATION_ACTIONS_PER_FAILURE:
        return None

    force = _set_force_repair_action(
        state,
        reason="exploration budget exhausted for the same unresolved failure without a file-changing repair",
        path=None,
        blocked_tool=tool,
        allowed_tools=["edit_file", "write_file", "run_tests", "finish"],
        extra={
            "trigger": "generic_exploration_budget",
            "exploration_count": count + 1,
            "max_exploration_actions": MAX_EXPLORATION_ACTIONS_PER_FAILURE,
        },
    )
    return ToolResult(
        tool=tool,
        ok=False,
        message=(
            "exploration budget exhausted for this unresolved failure; choose edit_file, "
            "write_file, run_tests, or finish with a concrete reason"
        ),
        data={
            "blocked_by_repair_action_budget": True,
            "blocked_by_exploration_budget": True,
            "force_repair_action": force,
            "attempted_tool": tool,
            "attempted_path": _path_from_args(args, state),
            "attempted_command": args.get("command") if isinstance(args, dict) else None,
        },
    )
def _record_patch(state: dict, result: dict) -> None:
    data = result.get("data", {}) or {}
    if not data.get("changed"):
        return
    diff = data.get("diff") or ""
    path = data.get("path") or "unknown"
    patches_dir = Path(state.get("patches_dir") or (Path(state["run_dir"]) / "patches"))
    patches_dir.mkdir(parents=True, exist_ok=True)
    idx = len(state.get("repair_history", [])) + 1
    safe_name = str(path).replace("/", "__").replace("\\", "__")
    diff_path = patches_dir / f"round_{state.get('round_idx',0):03d}_{idx:03d}_{safe_name}.diff"
    if diff:
        diff_path.write_text(diff, encoding="utf-8")
    hist = state.setdefault("repair_history", [])
    hist.append({
        "round_idx": state.get("round_idx", 0),
        "mode": state.get("mode"),
        "strategy": result.get("tool"),
        "changed": True,
        "files_changed": [path],
        "before_sha16": data.get("before_sha16"),
        "after_sha16": data.get("after_sha16"),
        "diff_path": str(diff_path) if diff else None,
        "message": result.get("message", ""),
    })


def _ban_action(state: dict, key: str, reason: str) -> None:
    banned = state.setdefault("banned_actions", [])
    item = {"action_key": key, "reason": reason, "round_idx": state.get("round_idx", 0)}
    if item not in banned:
        banned.append(item)


def _normalize_rel(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _path_kind_for_artifact(rel: str | None) -> str | None:
    norm = _normalize_rel(rel)
    if not norm:
        return None
    if is_test_like_path(norm):
        return "test"
    suffix = Path(norm).suffix.lower()
    if suffix == ".py":
        return "code"
    if suffix in {".md", ".rst", ".txt"}:
        return "docs"
    if suffix in {".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv"}:
        return "data"
    return None


def _declared_artifact_kind(state: dict, rel: str | None) -> str | None:
    norm = _normalize_rel(rel)
    if not norm:
        return None
    sources = [
        state.get("generated_files") or [],
        (state.get("file_plan") or {}).get("files") or [],
        (state.get("file_plan_review") or {}).get("writable_files") or [],
    ]
    for items in sources:
        for item in items:
            if not isinstance(item, dict) or _normalize_rel(item.get("path")) != norm:
                continue
            kind = item.get("kind") or item.get("role") or item.get("verification_role") or item.get("artifact_role")
            if kind:
                return str(kind)
    return None


def _was_new_file_write(data: dict) -> bool:
    if "existed_before" in data:
        return data.get("existed_before") is False
    return not data.get("prewrite_backup") and data.get("before_sha16") in {None, "", EMPTY_SHA16}


def _upsert_generated_file(state: dict, item: dict) -> None:
    rel = _normalize_rel(item.get("path"))
    if not rel:
        return
    item = {**item, "path": rel}
    clearable_keys = {"failed_write"}
    generated = state.setdefault("generated_files", [])
    for idx, existing in enumerate(generated):
        if isinstance(existing, dict) and _normalize_rel(existing.get("path")) == rel:
            merged = dict(existing)
            for key, value in item.items():
                if value is not None or key in clearable_keys:
                    merged[key] = value
            generated[idx] = merged
            return
    generated.append(item)


def _record_tool_write_artifact_state(state: dict, tool: str | None, result: dict) -> None:
    data = result.get("data", {}) or {}
    if tool not in {"write_file", "edit_file"} or not data.get("changed"):
        return
    rel = _normalize_rel(data.get("path"))
    if not rel:
        return

    changed_files = state.setdefault("changed_files", [])
    if rel not in [_normalize_rel(x) for x in changed_files]:
        changed_files.append(rel)

    kind = _declared_artifact_kind(state, rel) or _path_kind_for_artifact(rel)
    already_generated = _path_is_current_agent_generated(state, rel)
    if _was_new_file_write(data) or already_generated:
        _upsert_generated_file(
            state,
            {
                "path": rel,
                "ok": result.get("ok"),
                "message": result.get("message"),
                "syntax_check": data.get("syntax_check"),
                "failed_write": None if result.get("ok") else data.get("failed_write"),
                "kind": kind,
                "source": "tool_exec",
                "tool": tool,
                "verification_role": "test" if kind == "test" else None,
            },
        )
    if kind == "test":
        refresh_verification_test_registry(state, existing_only=False)



PATH_ARG_KEYS = ("path", "file_path", "filepath", "filename", "file", "target_file", "target_path")


def _path_from_args(args: dict, state: dict | None = None) -> str | None:
    for key in PATH_ARG_KEYS:
        if args.get(key):
            rel = _normalize_rel(str(args.get(key)))
            return rel
    return None


def _paths_from_read_args(tool: str, args: dict, state: dict | None = None) -> list[str]:
    if tool == "read_many_files" and isinstance(args.get("paths"), list):
        out: list[str] = []
        for item in args.get("paths") or []:
            rel = _normalize_rel(str(item))
            if rel and rel not in out:
                out.append(rel)
        return out
    rel = _path_from_args(args, state)
    return [rel] if rel else []


def _normalize_path_args(state: dict, args: dict) -> tuple[dict, dict | None]:
    return args, None


def _normalize_write_path_args(state: dict, tool: str | None, args: dict) -> tuple[dict, dict | None]:
    if tool not in {"write_file", "edit_file"} or not isinstance(args, dict):
        return args, None
    out = dict(args)
    changes: dict[str, dict[str, str]] = {}
    for key in PATH_ARG_KEYS:
        if not out.get(key):
            continue
        before = _normalize_rel(str(out[key]))
        test_mapped = mapped_agent_test_for_original(state, before)
        after = test_mapped or before
        if after != before:
            out[key] = after
            change = {"before": before, "after": after}
            change["reason"] = "mapped generated test path to agent internal test root"
            changes[key] = change
        break
    if not changes:
        return args, None
    return out, {"reason": "normalized write path before tool execution", "paths": changes}


def _as_list_arg(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _map_isolated_test_target(state: dict, target: str) -> tuple[str, dict | None]:
    raw = str(target).replace("\\", "/")
    path_part, sep, node_part = raw.partition("::")
    agent_test = mapped_agent_test_for_original(state, path_part)
    if agent_test and agent_test != _normalize_rel(path_part):
        mapped = agent_test + (sep + node_part if sep else "")
        return mapped, {"before": raw, "after": mapped, "reason": "mapped requested test path to agent internal test root"}
    rel = _normalize_rel(path_part)
    for item in state.get("generated_files") or []:
        if not isinstance(item, dict):
            continue
        original = _normalize_rel(item.get("original_path"))
        item_path = _normalize_rel(item.get("path"))
        if original and item_path and original == rel:
            mapped = item_path + (sep + node_part if sep else "")
            return mapped, {"before": raw, "after": mapped, "reason": "generated_file_original_path"}
    return raw, None


def _normalize_run_tests_args(state: dict, args: dict) -> tuple[dict, dict | None]:
    if not isinstance(args, dict):
        return args, None
    out = dict(args)
    targets = _as_list_arg(out.get("targets"))
    if not targets:
        targets = registered_test_paths(state, existing_only=True)
    mapped_targets: list[str] = []
    changes: list[dict] = []
    for target in targets:
        mapped, change = _map_isolated_test_target(state, target)
        if mapped and mapped not in mapped_targets:
            mapped_targets.append(mapped)
        if change:
            changes.append(change)
    if mapped_targets:
        out["targets"] = mapped_targets
    pythonpath = _as_list_arg(out.get("pythonpath"))
    if any(is_agent_test_path(target.split("::", 1)[0], state=state) for target in mapped_targets):
        if "." not in pythonpath:
            pythonpath.insert(0, ".")
        out["pythonpath"] = pythonpath
    if out == args:
        return args, None
    return out, {"reason": "mapped run_tests targets to agent internal test root", "changes": changes, "targets": out.get("targets"), "pythonpath": out.get("pythonpath")}


def _current_repair_write_targets(state: dict) -> list[str]:
    raw_targets: list[str] = []
    force = state.get("force_repair_action") or {}
    if force.get("required_path"):
        raw_targets.append(str(force.get("required_path")))
    for item in force.get("allowed_target_files") or []:
        raw_targets.append(str(item))

    targets: list[str] = []
    for raw in raw_targets:
        rel = _normalize_rel(raw)
        if rel and rel not in targets:
            targets.append(rel)
    return targets


def _force_allows_one_targeted_read(state: dict, tool: str, args: dict) -> bool:
    if tool != "read_file":
        return False
    force = state.get("force_repair_action") or {}
    if not force:
        return False
    rel = _normalize_rel(_path_from_args(args, state))
    if not rel:
        return False
    targets = set(_current_repair_write_targets(state))
    fallback = _normalize_rel(force.get("path"))
    if fallback:
        targets.add(fallback)
    targets.update(
        _normalize_rel(item)
        for item in force.get("allowed_read_files") or []
        if item
    )
    if targets and rel not in targets:
        return False
    return not request_is_cached(
        state,
        rel,
        args,
        current_sha16=_current_file_sha16(state, rel),
    )


def _repair_target_policy_result(state: dict, tool: str | None, args: dict) -> ToolResult | None:
    if tool not in {"write_file", "edit_file"} or not isinstance(args, dict):
        return None
    force = state.get("force_repair_action") or {}
    if not force.get("required_path"):
        return None
    targets = _current_repair_write_targets(state)
    if not targets:
        return None
    rel = _path_from_args(args, state)
    rel = _normalize_rel(rel)
    if rel in targets:
        return None
    force = _set_force_repair_action(
        state,
        reason="repair action attempted to write outside the repair controller target file",
        path=targets[0] if targets else None,
        blocked_tool=tool or "write_file",
        allowed_tools=["edit_file", "write_file", "run_tests", "finish"],
        extra={
            "required_path": targets[0] if targets else None,
            "allowed_target_files": targets,
            "attempted_path": rel,
            "trigger": "write_outside_repair_target",
        },
    )
    return ToolResult(
        tool=tool or "unknown",
        ok=False,
        message="repair write target blocked; write to the repair controller target file",
        data={
            "blocked_by_repair_target_policy": True,
            "path": rel,
            "allowed_target_files": targets,
            "force_repair_action": force,
            "repair_controller": state.get("repair_controller"),
            "strategy_decision": state.get("strategy_decision"),
        },
    )


def _normalize_write_path_for_project_layout(tool: str | None, state: dict, args: dict) -> tuple[dict, dict | None]:
    if tool != "write_file" or not isinstance(args, dict):
        return args, None
    rel = _path_from_args(args, state)
    if not rel:
        return args, None
    after, normalization = normalize_generated_test_write_path(state, rel)
    if not normalization or after == rel:
        return args, None
    out = dict(args)
    for key in PATH_ARG_KEYS:
        if out.get(key):
            out[key] = after
            break
    return out, normalization


def _redirect_external_test_edit_to_generated_test(tool: str | None, state: dict, args: dict) -> tuple[str | None, dict, dict | None]:
    if tool != "edit_file" or not isinstance(args, dict):
        return tool, args, None
    rel = _path_from_args(args, state)
    if not rel:
        return tool, args, None
    target = Path(state.get("workspace", "")) / rel
    if not target.is_file():
        return tool, args, None
    after, normalization = normalize_generated_test_write_path(state, rel)
    if not normalization or after == rel:
        return tool, args, None

    try:
        current = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return tool, args, None

    old_text = args.get("old_text")
    new_text = args.get("new_text")
    if not isinstance(new_text, str):
        return tool, args, None
    if isinstance(old_text, str) and old_text and old_text in current:
        content = current.replace(old_text, new_text, 1)
        content_source = "current file with edit replacement applied"
    else:
        content = new_text
        content_source = "new_text used as generated test content"

    new_args = {"path": after, "content": content}
    if "create_dirs" in args:
        new_args["create_dirs"] = args["create_dirs"]
    return "write_file", new_args, {
        **normalization,
        "from_tool": "edit_file",
        "to_tool": "write_file",
        "source_path": rel,
        "content_source": content_source,
        "reason": "redirected external test edit to a new generated test file",
    }


def _coerce_full_content_edit(tool: str | None, args: dict) -> tuple[str | None, dict, dict | None]:
    """Treat edit_file(path, content=...) as a full-file write_file call.

    Smaller models often choose edit_file while providing a complete file body.
    Returning a schema error repeatedly wastes repair rounds; write policy still
    gates the resulting write_file call, so this conversion is safe and generic.
    """
    if tool != "edit_file":
        return tool, args, None
    if "content" not in args or args.get("old_text") is not None or args.get("new_text") is not None:
        return tool, args, None
    path = _path_from_args(args)
    if not path:
        return tool, args, None
    new_args = {"path": path, "content": args.get("content")}
    if "create_dirs" in args:
        new_args["create_dirs"] = args["create_dirs"]
    return "write_file", new_args, {
        "from_tool": "edit_file",
        "to_tool": "write_file",
        "reason": "edit_file was given full file content without old_text/new_text",
    }



READ_TOOLS = set(REGISTRY_READ_TOOLS)
WRITE_TOOLS = set(REGISTRY_WRITE_TOOLS)


def _same_failure_key(state: dict) -> str:
    failure = state.get("failure") or {}
    return str(failure.get("signature") or failure.get("failure_type") or "no_failure")


def _current_file_sha16(state: dict, rel: str | None) -> str | None:
    if not rel:
        return None
    try:
        root = Path(state["workspace"]).resolve()
        p = (root / rel).resolve()
        if not is_within_workspace(root, p) or not p.is_file():
            return None
        return sha16(p.read_bytes())
    except Exception:
        return None


def _set_force_repair_action(
    state: dict,
    *,
    reason: str,
    path: str | None,
    blocked_tool: str,
    extra: dict | None = None,
    allowed_tools: list[str] | None = None,
) -> dict:
    force = dict(state.get("force_repair_action") or {})
    force.update({
        "reason": reason,
        "failure_signature": _same_failure_key(state),
        "path": path,
        "allowed_tools": allowed_tools or ["edit_file", "write_file", "run_tests", "run_shell", "finish"],
        "blocked_tool": blocked_tool,
        "blocked_read_attempts": int(force.get("blocked_read_attempts", 0)) + 1,
        "instruction": (
            "Repeated reading has not resolved the current failure. Do not read/search/list again for this failure. "
            "Change strategy now: edit or rewrite the relevant file, run verification, or finish with a concrete reason."
        ),
    })
    if extra:
        force.update(extra)
    state["force_repair_action"] = force
    state["repair_action_budget"] = {
        "version": "v1.19",
        "active": True,
        "force_repair_action": force,
        "read_cache": state.get("repair_read_cache", {}),
        "read_budget": state.get("repair_read_budget", {}),
    }
    return force


def _force_repair_policy_result(state: dict, tool: str, args: dict) -> ToolResult | None:
    force = state.get("force_repair_action") or {}
    if not force:
        return None
    allowed = set(force.get("allowed_tools") or [])
    required = force.get("required_tool")
    if tool == "finish":
        return None
    if tool == "write_file":
        rel = _normalize_rel(_path_from_args(args, state))
        target = (Path(str(state.get("workspace") or ".")) / rel) if rel else None
        if (
            rel
            and target is not None
            and target.is_file()
            and not _path_is_current_agent_generated(state, rel)
        ):
            return ToolResult(
                tool=tool,
                ok=False,
                message=(
                    "full-file rewrite of existing project source is blocked during repair; "
                    "use edit_file with exact replacements"
                ),
                data={
                    "blocked_by_existing_source_rewrite_policy": True,
                    "force_repair_action": force,
                    "attempted_path": rel,
                    "allowed_tools": sorted((allowed - {"write_file"}) | {"edit_file"}),
                },
            )
    if required and tool != required:
        return ToolResult(
            tool=tool,
            ok=False,
            message=f"repair controller requires {required} before more exploration",
            data={
                "blocked_by_repair_action_budget": True,
                "force_repair_action": force,
                "required_tool": required,
                "allowed_tools": sorted(allowed),
                "attempted_tool": tool,
                "attempted_path": _path_from_args(args, state),
            },
        )
    blocked = {str(item) for item in force.get("blocked_tools") or [] if str(item)}
    if tool in blocked:
        force["blocked_read_attempts"] = int(force.get("blocked_read_attempts", 0)) + 1
        state["force_repair_action"] = force
        return ToolResult(
            tool=tool,
            ok=False,
            message=f"repair controller blocks {tool} for this unresolved failure",
            data={
                "blocked_by_repair_action_budget": True,
                "blocked_by_force_blocked_tool": True,
                "force_repair_action": force,
                "blocked_tools": sorted(blocked),
                "allowed_tools": sorted(allowed),
                "attempted_tool": tool,
                "attempted_path": _path_from_args(args, state),
            },
        )
    if not required and _force_allows_one_targeted_read(state, tool, args):
        return None
    required_path = force.get("required_path")
    if required_path and tool in {"write_file", "edit_file"}:
        attempted = _path_from_args(args, state)
        attempted = _normalize_rel(attempted)

        allowed_paths: set[str] = set()
        for raw in [required_path, *(force.get("allowed_target_files") or [])]:
            rel = _normalize_rel(str(raw))
            if rel:
                allowed_paths.add(rel)
        if allowed_paths and attempted not in allowed_paths:
            return ToolResult(
                tool=tool,
                ok=False,
                message="repair controller requires writing the active repair target file before other files",
                data={
                    "blocked_by_repair_action_budget": True,
                    "blocked_by_force_repair_path": True,
                    "force_repair_action": force,
                    "allowed_target_files": sorted(allowed_paths),
                    "attempted_path": attempted,
                    "attempted_tool": tool,
                },
            )
    if required and tool == required:
        return None
    if not required and (not allowed or tool in allowed):
        return None
    return ToolResult(
        tool=tool,
        ok=False,
        message=f"repair controller requires {required or sorted(allowed)} before more exploration",
        data={
            "blocked_by_repair_action_budget": True,
            "force_repair_action": force,
            "required_tool": required,
            "allowed_tools": sorted(allowed),
            "attempted_tool": tool,
            "attempted_path": _path_from_args(args, state),
        },
    )


def _read_budget_policy_result(state: dict, tool: str, args: dict) -> ToolResult | None:
    if tool not in READ_TOOLS:
        return None
    force = state.get("force_repair_action") or {}
    if force and tool in READ_TOOLS:
        if _force_allows_one_targeted_read(state, tool, args):
            return None
        force = _set_force_repair_action(
            state,
            reason=str(force.get("reason") or "read action blocked by active repair controller"),
            path=force.get("path") or _path_from_args(args, state),
            blocked_tool=tool,
        )
        return ToolResult(
            tool=tool,
            ok=False,
            message=(
                "repeated read/explore action blocked by repair controller; do not read again for this failure. "
                "Choose edit_file, write_file, run_tests, run_shell, or finish with a concrete reason"
            ),
            data={"blocked_by_repair_action_budget": True, "force_repair_action": force},
        )
    if not state.get("failure"):
        return None
    read_paths = _paths_from_read_args(tool, args, state)
    if not read_paths:
        return None
    sig = _same_failure_key(state)
    budgets = state.setdefault("repair_read_budget", {})
    cache = state.setdefault("repair_read_cache", {})

    # Single-file reads can use exact unchanged-file cache data in the next
    # prompt. Batch reads are handled by the per-path budget below because a
    # model can otherwise evade guards by changing order or adding one path.
    if tool == "read_file":
        rel = read_paths[0]
        current_sha = _current_file_sha16(state, rel)
        key = cache_key(state, rel)
        cached = cache.get(key) or {}
        if request_is_cached(state, rel, args, current_sha16=current_sha):
            cached["blocked_repeats"] = int(cached.get("blocked_repeats", 0)) + 1
            cache[key] = cached
            force = _set_force_repair_action(
                state,
                reason="same unchanged line range was already read for this unresolved failure",
                path=rel,
                blocked_tool=tool,
                extra={"current_sha16": current_sha, "cached_round_idx": cached.get("round_idx")},
            )
            return ToolResult(
                tool=tool,
                ok=False,
                message="cached read already exists for this unchanged file and failure; choose edit_file, write_file, run_tests, run_shell, or finish",
                data={
                    "blocked_by_repair_action_budget": True,
                    "blocked_by_read_cache": True,
                    "force_repair_action": force,
                    "path": rel,
                    "current_sha16": current_sha,
                    "cached_reads": cached.get("reads") or [],
                },
            )

    counts = {rel: int(budgets.get(f"{sig}|{rel}", 0)) for rel in read_paths}
    over_budget = [rel for rel, count in counts.items() if count >= 2]
    # Allow at most two successful reads of a file for the same unresolved
    # failure. For read_many_files, block when every requested path is already
    # over budget; this still allows adding genuinely new files once, but stops
    # the common "read the same implementation files forever" loop.
    if counts and over_budget and (tool == "read_file" or len(over_budget) == len(read_paths)):
        rel = over_budget[0]
        force = _set_force_repair_action(
            state,
            reason="same file set repeatedly read for same unresolved failure" if tool == "read_many_files" else "same file repeatedly read for same unresolved failure",
            path=rel,
            blocked_tool=tool,
            extra={"read_counts": counts, "paths": read_paths},
        )
        return ToolResult(
            tool=tool,
            ok=False,
            message=(
                "requested file(s) have already been read twice for this unresolved failure; "
                "do not read again. Change strategy: patch, rewrite, verify, or finish with a concrete reason"
            ),
            data={"blocked_by_repair_action_budget": True, "force_repair_action": force, "path": rel, "read_counts": counts},
        )
    return None


def _path_is_current_agent_generated(state: dict, rel: str | None) -> bool:
    if not rel:
        return False
    norm = str(rel).replace("\\", "/")
    for item in state.get("generated_files") or []:
        if isinstance(item, dict) and str(item.get("path") or "").replace("\\", "/") == norm:
            return True
    return False


def _promote_failed_exact_edit_to_rewrite(state: dict, tool: str, args: dict, result: dict) -> None:
    if tool != "edit_file" or result.get("ok"):
        return
    data = result.get("data", {}) or {}
    if data.get("changed"):
        return
    rel = data.get("path") or _path_from_args(args, state)
    if not _path_is_current_agent_generated(state, rel):
        return
    message = str(result.get("message") or "")
    exact_edit_failed = (
        message.startswith("old_text not found")
        or message.startswith("expected ") and " replacements but found " in message
        or message.startswith("replacement ") and " expected " in message and " matches but found " in message
        or data.get("rejected_write")
    )
    if not exact_edit_failed:
        return
    force = _set_force_repair_action(
        state,
        reason="exact edit failed on a current-agent generated file; use full-file rewrite with write_file or finish with reason",
        path=rel,
        blocked_tool=tool,
        allowed_tools=["write_file", "run_tests", "run_shell", "finish"],
        extra={
            "required_tool": "write_file",
            "trigger": "failed_exact_edit_on_current_agent_file",
            "last_tool_message": message,
            "last_tool_data": data,
        },
    )
    state.setdefault("repair_history", []).append({
        "round_idx": state.get("round_idx", 0),
        "mode": state.get("mode"),
        "strategy": "force_full_file_rewrite",
        "changed": False,
        "files_changed": [],
        "message": force.get("reason"),
        "target_file": rel,
    })


def _record_rejected_write_draft(state: dict, tool: str, args: dict, result: dict) -> dict | None:
    if tool != "write_file" or result.get("ok"):
        return None
    data = result.get("data", {}) or {}
    if not (data.get("rejected_write") and (data.get("syntax_check") or {}).get("checked")):
        return None
    content = args.get("content")
    rel = data.get("path") or _path_from_args(args, state)
    if not isinstance(content, str) or not rel:
        return None
    record = record_failed_write(
        state,
        path=rel,
        content=content,
        tool=tool,
        result=result,
        source="tool_exec_node",
    )
    result.setdefault("data", {})["failed_write"] = record
    return record


def _record_successful_read_for_budget(state: dict, tool: str, args: dict, result: dict) -> None:
    if tool not in {"read_file", "read_many_files"} or not result.get("ok") or not state.get("failure"):
        return
    data = result.get("data", {}) or {}
    read_items: list[dict] = []
    if tool == "read_file":
        rel = _path_from_args(args, state)
        if rel:
            read_items.append({
                "path": rel,
                "sha16": data.get("sha16"),
                "start_line": data.get("start_line"),
                "end_line": data.get("end_line"),
                "total_lines": data.get("total_lines"),
                "content": data.get("content", ""),
            })
    else:
        for item in data.get("files") or []:
            if not isinstance(item, dict) or not item.get("ok"):
                continue
            rel = _normalize_rel(str(item.get("path") or ""))
            if rel:
                read_items.append({
                    "path": rel,
                    "sha16": item.get("sha16"),
                    "content": item.get("content", ""),
                    "chars": item.get("chars"),
                })
    if not read_items:
        return

    sig = _same_failure_key(state)
    budgets = state.setdefault("repair_read_budget", {})
    cache = state.setdefault("repair_read_cache", {})
    for item in read_items:
        rel = _normalize_rel(str(item.get("path") or ""))
        if not rel:
            continue
        key = f"{sig}|{rel}"
        budgets[key] = int(budgets.get(key, 0)) + 1
        current_sha = _current_file_sha16(state, rel)
        append_read_chunk(
            state,
            rel,
            {
                "path": rel,
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "total_lines": item.get("total_lines"),
                "sha16": item.get("sha16"),
                "chars": item.get("chars"),
                "content": truncate(str(item.get("content", "")), 16000),
            },
            current_sha16=current_sha,
            source_tool=tool,
        )
    state["repair_action_budget"] = {
        "version": "v1.19",
        "active": bool(state.get("force_repair_action")),
        "force_repair_action": state.get("force_repair_action"),
        "read_cache": state.get("repair_read_cache", {}),
        "read_budget": budgets,
    }


def _invalidate_read_cache_for_path(state: dict, rel: str | None) -> None:
    if not rel:
        return
    cache = state.get("repair_read_cache") or {}
    for key in list(cache.keys()):
        if key.endswith("|" + rel):
            cache.pop(key, None)
    state["repair_read_cache"] = cache
    if (state.get("force_repair_action") or {}).get("path") == rel:
        state["force_repair_action"] = None

def _modification_policy_result(state: dict, tool: str, args: dict) -> ToolResult | None:
    if tool in WRITE_TOOLS and (state.get("write_locked") or state.get("read_only")):
        rel = _path_from_args(args, state)
        return ToolResult(
            tool=tool,
            ok=False,
            message="global read-only/write-locked policy blocks write action",
            data={
                "blocked_by_policy": True,
                "read_only_violation": True,
                "path": rel,
                "read_only_policy": state.get("read_only_policy"),
            },
        )
    if tool not in {"write_file", "edit_file"}:
        return None
    rel = _path_from_args(args, state)
    if not rel:
        return None
    registry = build_artifact_registry(state)
    state["artifact_registry"] = registry
    artifact = artifact_for_path(registry, rel)
    exists = (Path(state["workspace"]) / rel).exists()
    ok, reason, details = can_execute_write_intent(state, rel, exists=exists)
    if ok:
        return None
    data = {"blocked_by_policy": True, "path": rel, "artifact": artifact, **details}
    if details.get("approval_required"):
        data["approval_required"] = True
    return ToolResult(tool=tool, ok=False, message=reason, data=data)

def tool_exec_node(state: dict) -> dict:
    trace = get_trace(state)
    decision = state.get("decision", {})
    action = decision.get("action", {})
    tool = action.get("tool")
    args = action.get("args", {}) or {}
    tool, args, coercion = _coerce_full_content_edit(tool, args)
    if coercion:
        action["tool"] = tool
        action["args"] = args
        decision["action"] = action
        state["decision"] = decision
        trace.event("tool_exec_action_coerced", coercion=coercion, action=action)
    args, path_normalization = _normalize_path_args(state, args)
    if path_normalization:
        action["args"] = args
        decision["action"] = action
        state["decision"] = decision
        trace.event("tool_exec_path_args_normalized", normalization=path_normalization, action=action)
    args, write_path_normalization = _normalize_write_path_args(state, tool, args)
    if write_path_normalization:
        action["args"] = args
        decision["action"] = action
        state["decision"] = decision
        trace.event("tool_exec_write_path_normalized", normalization=write_path_normalization, action=action)
    if tool == "run_tests":
        args, test_target_normalization = _normalize_run_tests_args(state, args)
        if test_target_normalization:
            action["args"] = args
            decision["action"] = action
            state["decision"] = decision
            trace.event("tool_exec_run_tests_args_normalized", normalization=test_target_normalization, action=action)
    tool, args, test_edit_redirect = _redirect_external_test_edit_to_generated_test(tool, state, args)
    if test_edit_redirect:
        action["tool"] = tool
        action["args"] = args
        decision["action"] = action
        state["decision"] = decision
        trace.event("tool_exec_external_test_edit_redirected", normalization=test_edit_redirect, action=action)
    args, layout_normalization = _normalize_write_path_for_project_layout(tool, state, args)
    if layout_normalization:
        action["args"] = args
        decision["action"] = action
        state["decision"] = decision
        trace.event("tool_exec_project_layout_path_normalized", normalization=layout_normalization, action=action)
    key = _action_key(tool, args)
    trace.event(
        "tool_call",
        tool=tool,
        args=args,
        tool_call_id=key,
        action_key=key,
        round_idx=state.get("round_idx", 0),
        mode=state.get("mode"),
        read_only=state.get("read_only"),
    )
    trace.event("tool_exec_start", tool=tool, args=args, mode=state.get("mode"), read_only=state.get("read_only"))

    history = state.setdefault("action_history", [])
    same_recent = [h for h in history[-4:] if h.get("action_key") == key and h.get("empty_result")]
    exploration_res = _generic_exploration_budget_result(state, tool, args)
    if exploration_res is not None:
        result = exploration_res.model_dump()
        state["blocked_steps"] = state.get("blocked_steps", []) + [int(state.get("plan_step_idx", 0))]
        _ban_action(state, key, "generic exploration budget exhausted without repair progress")
    else:
        repeat_res = _repeat_action_policy_result(state, tool, args, key)
    if exploration_res is not None:
        pass
    elif repeat_res is not None:
        result = repeat_res.model_dump()
        state["blocked_steps"] = state.get("blocked_steps", []) + [int(state.get("plan_step_idx", 0))]
        _ban_action(state, key, "repeated executable action without repair progress")
    elif len(same_recent) >= 2:
        result = ToolResult(
            tool=tool,
            ok=False,
            message="repeated empty action blocked; choose a different tool or finish",
            data={"blocked_by_repeated_action_guard": True, "action_key": key},
        ).model_dump()
        state["blocked_steps"] = state.get("blocked_steps", []) + [int(state.get("plan_step_idx", 0))]
        _ban_action(state, key, "repeated empty/no-useful-result action")
    else:
        force_res = _force_repair_policy_result(state, tool, args)
        if force_res is not None:
            result = force_res.model_dump()
        else:
            budget_res = _read_budget_policy_result(state, tool, args)
            if budget_res is not None:
                result = budget_res.model_dump()
            else:
                policy_res = _modification_policy_result(state, tool, args)
                if policy_res is not None:
                    result = policy_res.model_dump()
                else:
                    repair_target_res = _repair_target_policy_result(state, tool, args)
                    if repair_target_res is not None:
                        result = repair_target_res.model_dump()
                    else:
                        # If a write/edit is allowed to touch an existing file, preserve the
                        # full original file before the tool runs. This is separate from diffs
                        # because diffs may be truncated and projects may not be under git.
                        rel_for_backup = _path_from_args(args, state) if tool in {"write_file", "edit_file"} else None
                        target_for_backup = Path(state["workspace"]) / rel_for_backup if rel_for_backup else None
                        existed_before = bool(target_for_backup and target_for_backup.exists() and target_for_backup.is_file())
                        backup = prewrite_backup(state, rel_for_backup) if existed_before else None
                        allow_read_only_execution = bool(
                            state.get("mode") == "run_verify"
                            and tool in {"run_shell", "run_tests"}
                        )
                        res = execute_tool(
                            state["workspace"],
                            tool,
                            args,
                            read_only=bool(state.get("read_only", False)),
                            allow_read_only_execution=allow_read_only_execution,
                        )
                        result = res.model_dump()
                        if rel_for_backup:
                            result.setdefault("data", {})["existed_before"] = existed_before
                        if backup:
                            result.setdefault("data", {})["prewrite_backup"] = backup

    _record_successful_read_for_budget(state, tool, args, result)
    failed_write = _record_rejected_write_draft(state, tool, args, result)
    _promote_failed_exact_edit_to_rewrite(state, tool, args, result)

    if (not result.get("ok")) and ((result.get("data") or {}).get("tool_schema_error") or (result.get("data") or {}).get("missing_args") or (result.get("data") or {}).get("error_type")):
        _ban_action(state, key, "tool schema/execution error; LLM must correct tool call")

    state["last_tool_result"] = result
    if tool == "run_tests":
        data = result.get("data", {}) or {}
        run = {"name": "run_tests", **data}
        state["test_results"] = {
            "version": "run_tests_v1",
            "ok": bool(result.get("ok")),
            "runs": [run],
            "total": data.get("total", 0),
            "passed": data.get("passed", 0),
            "failed": data.get("failed", 0),
            "errors": data.get("errors", 0),
            "skipped": data.get("skipped", 0),
            "failures": data.get("failures", []),
            "issues": data.get("issues", []),
        }
        cmd_result = CommandResult(
            name="run_tests",
            command=data.get("command", []),
            returncode=int(data.get("returncode", 1)),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            timed_out=bool(data.get("timed_out", False)),
        )
        state["verification"] = VerificationResult(
            ok=bool(result.get("ok")),
            results=[cmd_result],
            test_results=state["test_results"],
            pytest_ok=bool(result.get("ok")),
        ).model_dump()
        state["needs_verification"] = not bool(result.get("ok"))
        state["verification_reason"] = "run_tests tool result"
        trace.event(
            "verification_result",
            ok=state["verification"].get("ok"),
            verification=state["verification"],
            test_results=state.get("test_results"),
            requirement_atom_check=state.get("requirement_atom_check"),
            requirement_atom_summary=state.get("requirement_atom_summary"),
            requirement_atom_status=requirement_atom_trace_status(state),
            source_tool="run_tests",
            tool_call_id=key,
        )
    empty = _empty_result(result)
    history.append({
        "round_idx": state.get("round_idx", 0),
        "tool": tool,
        "args": args,
        "action_key": key,
        "failure_signature": _same_failure_key(state) if state.get("failure") else "",
        "ok": result.get("ok"),
        "empty_result": empty,
        "changed": bool((result.get("data") or {}).get("changed")),
        "message": result.get("message", ""),
    })
    observations = state.setdefault("observations", [])
    observations.append({"round_idx": state.get("round_idx", 0), "decision": decision, "tool_result": result})
    if tool in {"write_file", "edit_file"}:
        data = result.get("data", {}) or {}
        syntax = data.get("syntax_check") or {}
        if syntax.get("checked") and not syntax.get("ok"):
            state["failure"] = {
                "failure_type": "syntax_level_error",
                "priority": 1,
                "message": syntax.get("message", "Python syntax check failed"),
                "target_file": data.get("path"),
                "signature": data.get("after_sha16") or "syntax_check",
                "raw_excerpt": str(syntax),
                "failed_write": failed_write or data.get("failed_write"),
                "source": "syntax_aware_file_tool",
            }
        if data.get("changed"):
            state.setdefault("progress_guard", {})["no_op_count"] = 0
            _record_patch(state, result)
            _invalidate_read_cache_for_path(state, data.get("path"))
            _record_tool_write_artifact_state(state, tool, result)
            update_implementation_batch(state)
            try:
                existed_before = bool(data.get("existed_before")) if "existed_before" in data else bool(data.get("prewrite_backup"))
                record_artifact_event(
                    state["workspace"],
                    path=data.get("path"),
                    thread_id=state.get("thread_id"),
                    task=state.get("task"),
                    action=tool,
                    origin="agent_modified" if existed_before else "agent_generated",
                    kind=_declared_artifact_kind(state, data.get("path")) or _path_kind_for_artifact(data.get("path")),
                    before_sha16=data.get("before_sha16"),
                    after_sha16=data.get("after_sha16"),
                )
            except Exception:
                pass
            state["needs_verification"] = True
            state["verification_reason"] = f"{tool} changed {data.get('path')}; execution verification is required"
            if syntax.get("checked") and not syntax.get("ok"):
                pass
            else:
                # A successful file-changing action invalidates any previous active failure; verifier may set a new one.
                state["failure"] = None
        else:
            guard = state.setdefault("progress_guard", {})
            guard["no_op_count"] = int(guard.get("no_op_count", 0)) + 1
            _ban_action(state, key, "write/edit produced no file change")
            state.setdefault("repair_history", []).append({
                "round_idx": state.get("round_idx", 0),
                "mode": state.get("mode"),
                "strategy": tool,
                "changed": False,
                "files_changed": [],
                "message": result.get("message", "no-op"),
            })
    if result.get("ok") and not empty:
        step = int(state.get("plan_step_idx", 0))
        if step not in state.get("completed_steps", []):
            state.setdefault("completed_steps", []).append(step)
        state["plan_step_idx"] = step + 1
    trace.event(
        "tool_result",
        tool=tool,
        ok=result.get("ok"),
        result=result,
        empty_result=empty,
        tool_call_id=key,
        action_key=key,
        round_idx=state.get("round_idx", 0),
        needs_verification=state.get("needs_verification"),
        verification_reason=state.get("verification_reason"),
    )
    trace.event("tool_exec_done", result=result, empty_result=empty, needs_verification=state.get("needs_verification"), verification_reason=state.get("verification_reason"))
    state["round_idx"] = int(state.get("round_idx", 0)) + 1
    trace.snapshot(state)
    return state
