from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coding_agent.scope.write_scope import extract_mentioned_paths, normalize_rel
from coding_agent.scope.write_guard import is_protected_existing_file, path_has_explicit_create_intent, path_has_read_reference_intent, is_test_path
from coding_agent.contracts.artifact_constraints import detect_prohibited_artifacts, is_prohibited_artifact_path
from coding_agent.memory.artifact_provenance import artifact_record_for_path
from coding_agent.memory.workspace_baseline import baseline_record_for_path, load_workspace_baseline
from coding_agent.workspace.run_paths import internal_generated_tests_enabled, project_memory_dir_for
from coding_agent.scope.scope_contract import build_scope_contract, path_allows_modify, path_is_protected, path_operation

DANGEROUS_WRITE_WORDS = ["覆盖", "重写", "替换", "overwrite", "replace existing", "rewrite existing"]
MODIFY_WORDS = ["修改", "修复", "改", "patch", "modify", "fix", "edit"]


def _norm(path: str | None) -> str:
    return normalize_rel(path)


def _near(task: str, path: str, window: int = 80) -> str:
    if not task or not path or path not in task:
        return ""
    i = task.find(path)
    return task[max(0, i - window): i + len(path) + window]


def _explicit_overwrite(task: str, path: str) -> bool:
    s = _near(task or "", path)
    low = s.lower()
    return any(w in s or w in low for w in DANGEROUS_WRITE_WORDS)


def _explicit_modify(task: str, path: str) -> bool:
    s = _near(task or "", path)
    low = s.lower()
    return any(w in s or w in low for w in MODIFY_WORDS)


def _kind_from_path(path: str, planned_kind: str | None = None) -> str:
    p = Path(_norm(path))
    name = p.name.lower()
    rel = _norm(path)
    if is_test_path(rel):
        return "test"
    if planned_kind in {"code", "test", "readme", "config", "data", "other"}:
        return planned_kind
    if name.startswith("readme") or p.suffix.lower() in {".md", ".rst"}:
        return "readme"
    if p.suffix.lower() == ".py":
        return "code"
    if p.suffix.lower() in {".json", ".jsonl", ".csv", ".yaml", ".yml", ".toml"}:
        return "data_or_config"
    return "other"


def _safe_unresolved_support_path(rel: str) -> bool:
    rel = _norm(rel)
    if not rel or rel.startswith("/") or rel.startswith(".coding_agent/"):
        return False
    if ".." in Path(rel).parts:
        return False
    if is_test_path(rel):
        return False
    return Path(rel).suffix.lower() in {
        ".py",
        ".toml",
        ".ini",
        ".cfg",
        ".yaml",
        ".yml",
        ".json",
        ".md",
        ".rst",
        ".txt",
    }


def _unresolved_modify_target_for_path(state: dict[str, Any], rel: str) -> dict[str, Any] | None:
    rel = _norm(rel)
    sources = [
        state.get("scope_contract") or {},
        (state.get("task_intent") or {}).get("scope_contract") or {},
        (state.get("task_intent") or {}).get("semantic_write_scope") or {},
    ]
    for source in sources:
        for item in source.get("unresolved_modify_targets") or []:
            if _norm((item or {}).get("path")) == rel:
                return dict(item)
        for item in source.get("path_operations") or []:
            if _norm((item or {}).get("path")) == rel and str((item or {}).get("operation") or "") == "unresolved_modify_target":
                return dict(item)
    return None


def classify_path_mention(task: str, path: str) -> dict[str, Any]:
    rel = _norm(path)
    scope = build_scope_contract(task or "", [rel])
    scope_op = path_operation(scope, rel)
    create = path_has_explicit_create_intent(task or "", rel)
    read_ref = path_has_read_reference_intent(task or "", rel)
    modify = _explicit_modify(task or "", rel)
    overwrite = _explicit_overwrite(task or "", rel)
    # If the same wide local window contains both create and read words, prefer
    # create for explicit code/readme output paths. This handles sentences like
    # "新增 scripts/foo.py。脚本应读取 data/input.json" where the read word is
    # near the first path but semantically applies to the second path.
    if scope_op == "forbid_modify":
        intent = "protected_reference"
    elif scope_op == "allow_modify":
        intent = "modify_target"
        modify = True
    elif create and (Path(rel).suffix.lower() in {".py", ".md", ".sh"} or rel.startswith("tests/")):
        intent = "create_target"
    elif read_ref and not overwrite:
        intent = "read_reference"
    elif create:
        intent = "create_target"
    elif modify or overwrite:
        intent = "modify_target"
    else:
        intent = "ambiguous_reference"
    return {
        "path": rel,
        "intent": intent,
        "create_intent": create,
        "read_reference_intent": read_ref,
        "modify_intent": modify,
        "overwrite_intent": overwrite,
        "scope_operation": scope_op,
        "protected_existing": scope_op == "forbid_modify",
        "context": _near(task or "", rel),
    }


def classify_task_paths(task: str) -> list[dict[str, Any]]:
    return [classify_path_mention(task or "", p) for p in extract_mentioned_paths(task or "")]


def _task_intent_path_mentions(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return path intents from the deterministic TaskIntentResolver.

    The lower-level local-window classifier is intentionally conservative, but
    it can be confused by a create target followed by scoped prohibitions such
    as "do not modify existing tests". The task intent resolver has the full
    prompt-level decision and inferred test targets, so it must win for explicit
    create/read-reference paths.
    """
    intent = state.get("task_intent") or ((state.get("supervisor") or {}).get("task_intent") or {})
    out: dict[str, dict[str, Any]] = {}

    for mention in intent.get("path_mentions") or []:
        rel = _norm(mention.get("path"))
        if not rel:
            continue
        m_intent = mention.get("intent")
        if m_intent == "create_target":
            out[rel] = {
                "path": rel,
                "intent": "create_target",
                "create_intent": True,
                "read_reference_intent": False,
                "modify_intent": False,
                "overwrite_intent": False,
                "context": mention.get("context", ""),
                "source": "task_intent.path_mentions",
            }
        elif m_intent == "modify_target":
            out[rel] = {
                "path": rel,
                "intent": "modify_target",
                "create_intent": False,
                "read_reference_intent": False,
                "modify_intent": True,
                "overwrite_intent": False,
                "context": mention.get("context", ""),
                "source": "task_intent.path_mentions",
                "scope_operation": mention.get("scope_operation"),
            }
        elif m_intent in {"protected_reference", "forbidden_modify"}:
            out[rel] = {
                "path": rel,
                "intent": "protected_reference",
                "create_intent": False,
                "read_reference_intent": True,
                "modify_intent": False,
                "overwrite_intent": False,
                "context": mention.get("context", ""),
                "source": "task_intent.path_mentions",
                "scope_operation": mention.get("scope_operation") or "forbid_modify",
                "protected_existing": True,
            }
        elif m_intent == "read_reference" and rel not in out:
            out[rel] = {
                "path": rel,
                "intent": "read_reference",
                "create_intent": False,
                "read_reference_intent": True,
                "modify_intent": False,
                "overwrite_intent": False,
                "context": mention.get("context", ""),
                "source": "task_intent.path_mentions",
            }

    for rel in intent.get("read_reference_paths") or []:
        rel = _norm(rel)
        if rel and rel not in out:
            out[rel] = {
                "path": rel,
                "intent": "read_reference",
                "create_intent": False,
                "read_reference_intent": True,
                "modify_intent": False,
                "overwrite_intent": False,
                "context": "task_intent.read_reference_paths",
                "source": "task_intent.read_reference_paths",
            }

    for rel in intent.get("allowed_modify_paths") or []:
        rel = _norm(rel)
        if rel:
            out[rel] = {
                "path": rel,
                "intent": "modify_target",
                "create_intent": False,
                "read_reference_intent": False,
                "modify_intent": True,
                "overwrite_intent": False,
                "context": "task_intent.allowed_modify_paths",
                "source": "task_intent.allowed_modify_paths",
                "scope_operation": "allow_modify",
            }

    for rel in intent.get("protected_existing_paths") or []:
        rel = _norm(rel)
        if rel:
            out[rel] = {
                "path": rel,
                "intent": "protected_reference",
                "create_intent": False,
                "read_reference_intent": True,
                "modify_intent": False,
                "overwrite_intent": False,
                "context": "task_intent.protected_existing_paths",
                "source": "task_intent.protected_existing_paths",
                "scope_operation": "forbid_modify",
                "protected_existing": True,
            }

    for rel in intent.get("create_paths") or []:
        rel = _norm(rel)
        if not rel:
            continue
        out[rel] = {
            "path": rel,
            "intent": "create_target",
            "create_intent": True,
            "read_reference_intent": False,
            "modify_intent": False,
            "overwrite_intent": False,
            "context": "task_intent.create_paths",
            "source": "task_intent.create_paths",
        }
    return out


def _provenance_kind(workspace: str, rel: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    prov = artifact_record_for_path(workspace, rel)
    base = baseline_record_for_path(workspace, rel)
    if prov and prov.get("created_by_agent"):
        return "agent_artifact", prov, base
    if base:
        return "project_existing", prov, base
    return "untracked_or_new", prov, base


def _make_intent(path: str, operation: str, allowed: bool, reason: str, *, role: str, source: str, risk: str = "low", **extra: Any) -> dict[str, Any]:
    d = {
        "path": _norm(path),
        "operation": operation,
        "allowed": bool(allowed),
        "reason": reason,
        "role": role,
        "source": source,
        "risk": risk,
        "backup_required": operation == "modify_existing" and allowed,
    }
    d.update(extra)
    return d


def build_write_intents(state: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    workspace = state["workspace"]
    task = state.get("task", "")
    read_only = bool(state.get("read_only"))
    mode = state.get("mode")
    plan = plan or state.get("file_plan") or {}
    root = Path(workspace).resolve()
    baseline = load_workspace_baseline(workspace)
    prohibited_artifacts = (
        (state.get("task_contract") or {}).get("prohibited_artifacts")
        or (state.get("task_intent") or {}).get("prohibited_artifacts")
        or detect_prohibited_artifacts(task)
    )
    scope_contract = state.get("scope_contract") or (state.get("task_intent") or {}).get("scope_contract") or build_scope_contract(task)
    mentions = {m["path"]: m for m in classify_task_paths(task)}
    mentions.update(_task_intent_path_mentions(state))
    intents: list[dict[str, Any]] = []
    seen: set[str] = set()

    # First record every user-mentioned path, including read references. This
    # makes audit and policy decisions explicit.
    for rel, mention in mentions.items():
        exists = (root / rel).exists()
        original_rel = _norm(mention.get("original_path"))
        origin, prov, base = _provenance_kind(workspace, rel)
        role = _kind_from_path(rel)
        protected = exists and is_protected_existing_file(rel)
        protected_by_scope = path_is_protected(
            scope_contract,
            rel,
            original_path=original_rel,
            include_globs=exists,
        )
        explicit_modify_allowed = path_allows_modify(scope_contract, rel, original_path=original_rel)
        prohibited = is_prohibited_artifact_path(rel, prohibited_artifacts)
        if prohibited:
            intent = _make_intent(rel, "create_new" if not exists else "modify_existing", False, "artifact kind is prohibited by user task", role=role, source="prohibited_artifact_constraint", risk="high", origin=origin, provenance=prov, baseline=base, mention=mention)
        elif explicit_modify_allowed and exists and not read_only:
            intent = _make_intent(rel, "modify_existing", True, "user explicitly allowed modifying this path", role=role, source="scope_contract", risk="medium", origin=origin, provenance=prov, baseline=base, mention=mention)
        elif protected_by_scope or mention.get("intent") == "protected_reference":
            intent = _make_intent(rel, "read_reference" if not exists else "modify_existing", False, "path is protected by explicit task scope", role=role, source="scope_contract", risk="protected", origin=origin, provenance=prov, baseline=base, mention=mention)
        elif mention["intent"] == "read_reference":
            intent = _make_intent(rel, "read_reference", False, "user-mentioned input/reference path; read only", role=role, source="task_path_mention", risk="protected" if protected else "low", origin=origin, provenance=prov, baseline=base, mention=mention)
        elif mention["intent"] == "create_target":
            if read_only:
                intent = _make_intent(rel, "create_new" if not exists else "modify_existing", False, "global read-only policy", role=role, source="task_path_mention", risk="high", origin=origin, provenance=prov, baseline=base, mention=mention)
            elif exists:
                if origin == "agent_artifact" and not protected:
                    intent = _make_intent(rel, "modify_existing", True, "target exists and is historical agent artifact", role=role, source="task_path_mention", origin=origin, provenance=prov, baseline=base, mention=mention)
                elif protected and not mention.get("overwrite_intent"):
                    intent = _make_intent(rel, "read_reference", False, "existing protected data/result/config path must not be overwritten", role=role, source="task_path_mention", risk="protected", origin=origin, provenance=prov, baseline=base, mention=mention)
                else:
                    # Existing project source is not automatically overwritten by a create request.
                    intent = _make_intent(rel, "approval_required", False, "target exists but is not known agent artifact; needs approval before overwrite", role=role, source="task_path_mention", risk="approval", origin=origin, provenance=prov, baseline=base, mention=mention)
            else:
                intent = _make_intent(rel, "create_new", True, "user explicitly requested this new output path", role=role, source="task_path_mention", origin=origin, provenance=prov, baseline=base, mention=mention)
        elif mention["intent"] == "modify_target":
            if read_only:
                intent = _make_intent(rel, "modify_existing", False, "global read-only policy", role=role, source="task_path_mention", risk="high", origin=origin, provenance=prov, baseline=base, mention=mention)
            elif explicit_modify_allowed and exists:
                intent = _make_intent(rel, "modify_existing", True, "user explicitly allowed modifying this path", role=role, source="scope_contract", risk="medium", origin=origin, provenance=prov, baseline=base, mention=mention)
            elif exists and origin == "agent_artifact" and not protected:
                intent = _make_intent(rel, "modify_existing", True, "historical agent artifact may be modified", role=role, source="task_path_mention", origin=origin, provenance=prov, baseline=base, mention=mention)
            elif exists and protected and not mention.get("overwrite_intent"):
                intent = _make_intent(rel, "approval_required", False, "existing protected artifact requires approval before modification", role=role, source="task_path_mention", risk="approval", origin=origin, provenance=prov, baseline=base, mention=mention)
            else:
                allow = (mode in {"modify", "debug", "repair_existing"}) and role == "code" and not is_test_path(rel)
                intent = _make_intent(rel, "modify_existing", allow, "source modification allowed by mode" if allow else "modification requires approval or clearer scope", role=role, source="task_path_mention", risk="medium", origin=origin, provenance=prov, baseline=base, mention=mention)
        else:
            intent = _make_intent(rel, "read_reference", False, "ambiguous mentioned path defaults to read-reference, not output", role=role, source="task_path_mention", origin=origin, provenance=prov, baseline=base, mention=mention)
        intents.append(intent)
        seen.add(rel)

    # Then handle file plan entries. User-mentioned reference inputs remain read-only;
    # planned code/test/readme deliverables may become write targets.
    for item in (plan.get("files") or []):
        rel = _norm(item.get("path"))
        if not rel:
            continue
        if rel in seen:
            # A task mention may have classified the path as read_reference. Do
            # not let the LLM file_plan overturn that for protected/reference paths.
            continue
        exists = (root / rel).exists()
        original_rel = _norm(item.get("original_path"))
        origin, prov, base = _provenance_kind(workspace, rel)
        role = _kind_from_path(rel, item.get("kind"))
        protected = exists and is_protected_existing_file(rel)
        protected_by_scope = path_is_protected(
            scope_contract,
            rel,
            original_path=original_rel,
            include_globs=exists,
        )
        explicit_modify_allowed = path_allows_modify(scope_contract, rel, original_path=original_rel)
        prohibited = is_prohibited_artifact_path(rel, prohibited_artifacts)
        if prohibited:
            intent = _make_intent(rel, "create_new" if not exists else "modify_existing", False, "artifact kind is prohibited by user task", role=role, source="prohibited_artifact_constraint", risk="high", origin=origin, provenance=prov, baseline=base, plan_item=item)
        elif explicit_modify_allowed and exists and not read_only:
            intent = _make_intent(rel, "modify_existing", True, "user explicitly allowed modifying this path", role=role, source="scope_contract", risk="medium", origin=origin, provenance=prov, baseline=base, plan_item=item)
        elif protected_by_scope:
            intent = _make_intent(rel, "read_reference" if not exists else "modify_existing", False, "path is protected by explicit task scope", role=role, source="scope_contract", risk="protected", origin=origin, provenance=prov, baseline=base, plan_item=item)
        elif read_only:
            intent = _make_intent(rel, "create_new" if not exists else "modify_existing", False, "global read-only policy", role=role, source="file_plan", risk="high", origin=origin, provenance=prov, baseline=base, plan_item=item)
        elif exists:
            if origin == "agent_artifact" and not protected:
                intent = _make_intent(rel, "modify_existing", True, "file_plan target exists and is historical/current agent artifact", role=role, source="file_plan", origin=origin, provenance=prov, baseline=base, plan_item=item)
            elif protected:
                intent = _make_intent(rel, "approval_required", False, "file_plan attempted to modify protected existing artifact", role=role, source="file_plan", risk="approval", origin=origin, provenance=prov, baseline=base, plan_item=item)
            elif role == "code" and mode in {"modify", "debug", "repair_existing"}:
                intent = _make_intent(rel, "modify_existing", True, "existing source file allowed by modify/debug mode", role=role, source="file_plan", risk="medium", origin=origin, provenance=prov, baseline=base, plan_item=item)
            else:
                intent = _make_intent(rel, "approval_required", False, "existing non-agent file requires approval before overwrite", role=role, source="file_plan", risk="approval", origin=origin, provenance=prov, baseline=base, plan_item=item)
        else:
            if role in {"code", "test", "readme"} or item.get("kind") in {"code", "test", "readme"}:
                intent = _make_intent(rel, "create_new", True, "file_plan deliverable is a new code/test/readme artifact", role=role, source="file_plan", origin=origin, provenance=prov, baseline=base, plan_item=item)
            else:
                intent = _make_intent(rel, "read_reference", False, "planned non-code data/config artifact is not written unless explicitly requested", role=role, source="file_plan", risk="medium", origin=origin, provenance=prov, baseline=base, plan_item=item)
        intents.append(intent)
        seen.add(rel)

    by_path = {i["path"]: i for i in intents}
    allowed = [i for i in intents if i.get("allowed")]
    blocked = [i for i in intents if not i.get("allowed") and i.get("operation") not in {"read_reference"}]
    read_refs = [i for i in intents if i.get("operation") == "read_reference"]
    return {
        "version": "v1.17",
        "mode": mode,
        "read_only": read_only,
        "baseline_path": str(project_memory_dir_for(workspace) / "workspace_baseline.json"),
        "intents": intents,
        "by_path": by_path,
        "allowed_write_paths": [i["path"] for i in allowed],
        "blocked_write_paths": [i["path"] for i in blocked],
        "read_reference_paths": [i["path"] for i in read_refs],
        "baseline_file_count": len((baseline.get("files") or {})),
    }


def intent_for_path(state: dict[str, Any], path: str) -> dict[str, Any] | None:
    rel = _norm(path)
    registry = state.get("write_intents") or {}
    return (registry.get("by_path") or {}).get(rel)


def _current_agent_generated_path(state: dict[str, Any], rel: str, provenance: dict[str, Any] | None = None) -> bool:
    rel = _norm(rel)
    if not rel:
        return False
    thread_id = str(state.get("thread_id") or "")
    for item in state.get("generated_files") or []:
        if isinstance(item, dict) and _norm(item.get("path")) == rel:
            return True
    registry = state.get("artifact_registry") or {}
    for item in registry.get("entries") or []:
        if not isinstance(item, dict):
            continue
        if _norm(item.get("path")) == rel and (item.get("agent_generated") or item.get("origin") == "agent_generated_or_modified"):
            return True
    if provenance and provenance.get("created_by_agent"):
        if thread_id and provenance.get("first_seen_thread_id") == thread_id:
            return True
        if thread_id and provenance.get("last_thread_id") == thread_id:
            return True
    return False


def _successfully_read_paths(state: dict[str, Any]) -> set[str]:
    """Return project paths the agent inspected successfully in this run."""
    out: set[str] = set()
    for item in state.get("action_history") or []:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if item.get("tool") == "read_file":
            rel = _norm(args.get("path"))
            if rel:
                out.add(rel)
        elif item.get("tool") == "read_many_files":
            for value in args.get("paths") or []:
                rel = _norm(str(value))
                if rel:
                    out.add(rel)
    return out


def _source_area(path: str) -> str:
    parts = Path(_norm(path)).parts
    return parts[0] if len(parts) > 1 else "."


def _allow_read_grounded_scope_expansion(
    state: dict[str, Any],
    scope_contract: dict[str, Any],
    rel: str,
) -> tuple[bool, dict[str, Any]]:
    """Expand an LLM-proposed source scope only after bounded repository evidence.

    Explicit user path boundaries remain closed. An initial semantic scope
    inferred by the model is a hypothesis, however, and real repairs often
    reveal a neighboring implementation file after inspection. Such an
    expansion is limited to an existing, non-test Python file in the same
    source area that the agent has already read successfully.
    """
    expanded = {
        _norm(value)
        for value in scope_contract.get("expanded_modify_paths") or []
        if _norm(value)
    }
    if rel in expanded:
        return True, {"scope_expansion": True, "scope_expansion_existing": True}
    allowed = [
        _norm(value)
        for value in scope_contract.get("allowed_modify_paths") or []
        if _norm(value)
    ]
    mentioned = {_norm(path) for path in extract_mentioned_paths(str(state.get("task") or ""))}
    broad_repository_repair = bool(
        (state.get("task_intent") or {}).get("source_modify_intent")
        and str((state.get("task_completeness") or {}).get("target_clarity") or "") == "repository_discoverable"
        and not set(allowed).intersection(mentioned)
    )
    if (
        scope_contract.get("semantic_write_scope_source") != "llm"
        or not allowed
        or _kind_from_path(rel) != "code"
        or is_test_path(rel)
        or rel not in _successfully_read_paths(state)
        or (
            not broad_repository_repair
            and _source_area(rel) not in {_source_area(value) for value in allowed}
        )
    ):
        return False, {}

    evidence = {
        "path": rel,
        "reason": "successfully read source before write under repository-discoverable repair scope",
        "source": "runtime_read_grounded_scope_expansion",
        "allowed_source_area": _source_area(rel),
    }
    scope_contract.setdefault("expanded_modify_paths", []).append(rel)
    state.setdefault("scope_expansions", []).append(evidence)
    return True, {"scope_expansion": True, "scope_expansion_evidence": evidence}


def can_execute_write_intent(state: dict[str, Any], path: str, *, exists: bool) -> tuple[bool, str, dict[str, Any]]:
    rel = _norm(path)
    intent = intent_for_path(state, rel)
    workspace = state.get("workspace")
    origin, prov, base = _provenance_kind(workspace, rel) if workspace else ("unknown", None, None)
    role = _kind_from_path(rel)
    scope_contract = state.get("scope_contract") or (state.get("task_intent") or {}).get("scope_contract") or build_scope_contract(state.get("task", ""))
    original_rel = _norm((intent or {}).get("original_path") or ((intent or {}).get("mention") or {}).get("original_path") or ((intent or {}).get("plan_item") or {}).get("original_path"))
    if state.get("read_only"):
        return False, "global read-only policy blocks write", {"write_intent": intent, "origin": origin, "provenance": prov}
    prohibited_artifacts = (
        (state.get("task_contract") or {}).get("prohibited_artifacts")
        or (state.get("task_intent") or {}).get("prohibited_artifacts")
        or detect_prohibited_artifacts(state.get("task", ""))
    )
    if is_prohibited_artifact_path(rel, prohibited_artifacts):
        return False, "artifact kind is prohibited by user task", {
            "write_intent": intent,
            "origin": origin,
            "provenance": prov,
            "prohibited_artifacts": prohibited_artifacts,
        }
    if _current_agent_generated_path(state, rel, prov):
        return True, "current agent generated artifact may be repaired", {
            "write_intent": intent,
            "origin": origin,
            "provenance": prov,
            "current_agent_generated": True,
        }
    if path_is_protected(
        scope_contract,
        rel,
        original_path=original_rel,
        include_globs=exists,
    ):
        return False, "path is protected by explicit task scope", {
            "write_intent": intent,
            "origin": origin,
            "provenance": prov,
            "protected_by_scope": True,
        }
    allowed_modify_paths = {
        _norm(value)
        for value in scope_contract.get("allowed_modify_paths") or []
        if _norm(value)
    }
    if exists and allowed_modify_paths and rel not in allowed_modify_paths:
        expansion_allowed, expansion_detail = _allow_read_grounded_scope_expansion(
            state,
            scope_contract,
            rel,
        )
        if not expansion_allowed:
            return False, "existing source path is outside the allowed modification scope", {
                "write_intent": intent,
                "origin": origin,
                "provenance": prov,
                "outside_allowed_modify_scope": True,
                "allowed_modify_paths": sorted(allowed_modify_paths),
                "approval_required": False,
            }
        if intent and not intent.get("allowed"):
            return False, intent.get("reason", "write_intent denies this write"), {
                "write_intent": intent,
                "origin": origin,
                "provenance": prov,
                **expansion_detail,
                "approval_required": intent.get("operation") == "approval_required",
            }
        return True, "read-grounded neighboring source scope expansion allowed", {
            "write_intent": intent,
            "origin": origin,
            "provenance": prov,
            **expansion_detail,
        }
    if intent:
        if intent.get("allowed"):
            return True, intent.get("reason", "allowed by write_intent"), {"write_intent": intent, "origin": origin, "provenance": prov}
        return False, intent.get("reason", "write_intent denies this write"), {"write_intent": intent, "origin": origin, "provenance": prov, "approval_required": intent.get("operation") == "approval_required"}
    # No intent exists. Apply conservative defaults: create is allowed for
    # clearly new planned-less code/test files only in write/generate modes;
    # modifying existing files requires either provenance or debug source mode.
    protected = exists and is_protected_existing_file(rel)
    if exists:
        if origin == "agent_artifact" and not protected:
            return True, "existing historical agent artifact allowed without explicit intent", {"origin": origin, "provenance": prov, "implicit_agent_artifact": True}
        if protected:
            return False, "existing protected artifact has no write_intent", {"origin": origin, "provenance": prov, "approval_required": True, "protected_existing_file": True}
        if role == "code" and state.get("mode") in {"modify", "debug", "repair_existing"} and not is_test_path(rel):
            return True, "existing source code allowed by debug/modify mode", {"origin": origin, "provenance": prov, "implicit_source_modify": True}
        return False, "existing file has no allowed write_intent", {"origin": origin, "provenance": prov, "approval_required": True}
    # New file with no intent: allow only if it is a code/test/readme artifact in
    # a write/generate mode. Destructive data/result creation still requires an explicit plan.
    if (
        state.get("mode") in {"write", "generate_project"}
        and (role in {"code", "readme"} or (role == "test" and rel.startswith(".coding_agent_test/")))
    ):
        if role == "test" and rel.startswith(".coding_agent_test/") and not internal_generated_tests_enabled(state):
            return False, "internal generated tests are disabled by default", {
                "origin": origin,
                "internal_generated_tests_enabled": False,
            }
        return True, "new code/test/readme file allowed by write mode", {"origin": origin, "implicit_new_artifact": True}
    expected = set((state.get("task_contract") or {}).get("expected_artifacts") or [])
    task_low = str(state.get("task") or "").lower()
    if (
        state.get("mode") in {"modify", "debug", "repair_existing"}
        and role == "test"
        and rel.startswith(".coding_agent_test/")
        and ("tests" in expected or "pytest" in task_low or "test" in task_low or "测试" in str(state.get("task") or ""))
    ):
        if not internal_generated_tests_enabled(state):
            return False, "internal generated tests are disabled by default", {
                "origin": origin,
                "internal_generated_tests_enabled": False,
            }
        return True, "new test file allowed by modify/debug task verification scope", {"origin": origin, "implicit_new_test": True}
    unresolved = _unresolved_modify_target_for_path(state, rel)
    intent_state = state.get("task_intent") or {}
    source_modify_allowed = bool(
        intent_state.get("source_modify_intent")
        or intent_state.get("operation_mode") in {"scoped_modify", "repair_existing"}
        or state.get("mode") in {"modify", "debug", "repair_existing"}
    )
    if (
        state.get("mode") in {"modify", "debug", "repair_existing"}
        and source_modify_allowed
        and unresolved
        and _safe_unresolved_support_path(rel)
    ):
        return True, "new support file allowed by semantic modify scope", {
            "origin": origin,
            "implicit_support_create": True,
            "unresolved_modify_target": unresolved,
        }
    return False, "new file has no allowed write_intent", {"origin": origin, "provenance": prov, "approval_required": False}
