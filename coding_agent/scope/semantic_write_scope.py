from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.scope.write_scope import normalize_rel


VERSION = "semantic_write_scope_v2"
MIN_SOURCE_MODIFY_CONFIDENCE = 0.65

SOURCE_MODES = {
    "source_modify",
    "modify",
    "modify_code",
    "debug",
    "fix",
    "fix_tests",
    "repair",
    "repair_existing",
}
CREATE_MODES = {"safe_create", "write", "write_script", "create", "generate_file"}
READ_ONLY_MODES = {"read_only_analysis", "analyze", "inspect", "review"}
PROJECT_MODES = {"generate_project", "full_workspace"}

OPERATION_ALIASES = {
    "read": "read_reference",
    "reference": "read_reference",
    "input": "read_reference",
    "create": "create_new",
    "create_file": "create_new",
    "new": "create_new",
    "write_new": "create_new",
    "modify": "modify_existing",
    "edit": "modify_existing",
    "patch": "modify_existing",
    "update": "modify_existing",
    "allow_modify": "modify_existing",
    "delete_file": "delete",
    "remove": "delete",
}
VALID_OPERATIONS = {"read_reference", "create_new", "modify_existing", "delete", "unknown"}


WRITE_SCOPE_INTENT_SCHEMA = """
Return a structured write_scope_intent object. This is a semantic judgment, not a keyword match:
{
  "task_mode": "read_only_analysis|safe_create|source_modify|debug|generate_project|unknown",
  "source_modification": {"allowed": false, "confidence": 0.0, "reason": "..."},
  "existing_file_modification": {"allowed": false, "confidence": 0.0, "reason": "..."},
  "allowed_operations": [
    {"path": "relative/path.py", "operation": "read_reference|create_new|modify_existing|delete|unknown", "confidence": 0.0, "reason": "..."}
  ],
  "protected_paths": [
    {"path": "relative/path.py or glob", "reason": "..."}
  ],
  "ambiguities": ["..."],
  "confidence": 0.0,
  "reason": "short rationale"
}
Guidance:
- If the user wants existing project behavior changed, bugs fixed, or tests made to pass, source_modification.allowed should be true even when the prompt does not literally say "modify source".
- If the user asks for a separate new artifact, analysis script, report, or explicitly says not to edit existing project files, source_modification.allowed should be false and the new artifact should be represented as create_new.
- If source-write permission is ambiguous, use confidence below 0.65 and prefer safe_create over source_modify.
"""


def _extract_raw_write_scope(llm_obj: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(llm_obj, dict):
        return None
    if any(k in llm_obj for k in ("task_mode", "source_modification", "allowed_operations", "protected_paths")):
        return llm_obj
    for key in ("write_scope_intent", "semantic_write_scope", "source_write_intent"):
        raw = llm_obj.get(key)
        if isinstance(raw, dict):
            return raw
    raw = llm_obj.get("write_scope")
    if isinstance(raw, dict) and any(k in raw for k in ("task_mode", "source_modification", "allowed_operations")):
        return raw
    return None


def _as_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if isinstance(value, dict):
        return value
    if isinstance(value, bool):
        return {"allowed": value}
    return {}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "y", "1", "allowed", "required"}:
            return True
        if low in {"false", "no", "n", "0", "blocked", "forbidden"}:
            return False
    return None


def _confidence(*values: Any, default: float = 0.0) -> float:
    for value in values:
        try:
            if value is None:
                continue
            out = float(value)
            if out > 1.0:
                out = out / 100.0
            return max(0.0, min(1.0, out))
        except (TypeError, ValueError):
            continue
    return default


def _task_mode(raw: Any) -> str:
    mode = str(raw or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in SOURCE_MODES | CREATE_MODES | READ_ONLY_MODES | PROJECT_MODES:
        if mode in {"modify", "modify_code"}:
            return "source_modify"
        if mode in {"fix", "fix_tests", "repair", "repair_existing"}:
            return "debug"
        if mode in {"write", "write_script", "create", "generate_file"}:
            return "safe_create"
        if mode in {"analyze", "inspect", "review"}:
            return "read_only_analysis"
        if mode == "full_workspace":
            return "generate_project"
        return mode
    return "unknown"


def _operation(raw: Any) -> str:
    op = str(raw or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    op = OPERATION_ALIASES.get(op, op)
    return op if op in VALID_OPERATIONS else "unknown"


def _normalize_path(raw: Any, *, allow_glob: bool = False) -> str:
    raw_text = str(raw or "").strip().strip("'\"")
    if not allow_glob and raw_text.replace("\\", "/").endswith("/"):
        return ""
    path = normalize_rel(raw_text)
    if not path:
        return ""
    if path.startswith("./"):
        path = path[2:]
    if path.startswith("/") or ".." in Path(path).parts:
        return ""
    if path.startswith(".coding_agent/"):
        return ""
    if not allow_glob and "*" in path:
        return ""
    return normalize_rel(path)


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _path_from_protected_item(item: Any) -> tuple[str, str]:
    if isinstance(item, str):
        return _normalize_path(item, allow_glob=True), ""
    if not isinstance(item, dict):
        return "", ""
    raw_path = item.get("path") or item.get("glob") or item.get("pattern")
    return _normalize_path(raw_path, allow_glob=True), str(item.get("reason") or "")


def _creation_only_protection(path: str, reason: str) -> bool:
    """Recognize an LLM wildcard that prohibits creation, not existing edits."""
    if path not in {"**", "**/*", "*"}:
        return False
    low = str(reason or "").lower()
    return any(marker in low for marker in (
        "no new file",
        "do not add new",
        "must not create",
        "forbid creating",
        "禁止新增",
        "不得新增",
        "不能新增",
        "禁止创建",
        "不得创建",
    ))


def _iter_allowed_operations(raw: dict[str, Any]) -> list[dict[str, Any]]:
    value = raw.get("allowed_operations")
    if value is None:
        value = raw.get("path_operations")
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            path = _normalize_path(item)
            if path:
                out.append({"path": path, "operation": "unknown", "confidence": 0.0, "reason": ""})
            continue
        if not isinstance(item, dict):
            continue
        op = _operation(item.get("operation") or item.get("intent") or item.get("action"))
        path = _normalize_path(item.get("path") or item.get("target"))
        if not path:
            continue
        out.append(
            {
                "path": path,
                "operation": op,
                "confidence": _confidence(item.get("confidence"), default=0.0),
                "reason": str(item.get("reason") or item.get("evidence") or "")[:500],
            }
        )
    return out


def resolve_semantic_write_scope(
    task: str,
    llm_obj: dict[str, Any] | None,
    mentioned_paths: list[str] | None = None,
) -> dict[str, Any]:
    raw = _extract_raw_write_scope(llm_obj)
    base: dict[str, Any] = {
        "version": VERSION,
        "available": False,
        "valid": False,
        "source": "missing",
        "task_mode": "unknown",
        "operation_mode": "",
        "mode": "",
        "confidence": 0.0,
        "low_confidence": True,
        "source_modification_allowed": False,
        "existing_file_modification_allowed": False,
        "safety_downgraded": False,
        "consistency_issues": [],
        "allowed_modify_paths": [],
        "create_paths": [],
        "read_reference_paths": [],
        "protected_existing_paths": [],
        "protected_existing_globs": [],
        "path_operations": [],
        "ambiguities": [],
        "reason": "",
    }
    if raw is None:
        return base

    source_section = _as_section(raw, "source_modification")
    existing_section = _as_section(raw, "existing_file_modification")
    task_mode = _task_mode(raw.get("task_mode") or raw.get("mode"))
    confidence = _confidence(
        raw.get("confidence"),
        source_section.get("confidence"),
        existing_section.get("confidence"),
        default=0.0,
    )

    source_allowed_raw = _as_bool(source_section.get("allowed"))
    if source_allowed_raw is None:
        source_allowed_raw = _as_bool(raw.get("source_modification_allowed"))
    if source_allowed_raw is None:
        source_allowed_raw = task_mode in {"source_modify", "debug"}

    existing_allowed_raw = _as_bool(existing_section.get("allowed"))
    if existing_allowed_raw is None:
        existing_allowed_raw = _as_bool(raw.get("existing_file_modification_allowed"))
    if existing_allowed_raw is None:
        existing_allowed_raw = bool(source_allowed_raw)

    allowed_modify_paths: list[str] = []
    create_paths: list[str] = []
    read_reference_paths: list[str] = []
    path_operations: list[dict[str, Any]] = []
    for item in _iter_allowed_operations(raw):
        op = item["operation"]
        path = item["path"]
        if op == "modify_existing":
            _append_unique(allowed_modify_paths, path)
            scope_op = "allow_modify"
        elif op == "create_new":
            _append_unique(create_paths, path)
            scope_op = "create_new"
        elif op == "read_reference":
            _append_unique(read_reference_paths, path)
            scope_op = "read_reference"
        elif op == "delete":
            scope_op = "delete"
        else:
            scope_op = "mention"
        path_operations.append(
            {
                "path": path,
                "operation": scope_op,
                "evidence": item.get("reason", "")[:400],
                "source": "llm_write_scope_intent",
            }
        )

    protected_existing_paths: list[str] = []
    protected_existing_globs: list[str] = []
    for item in raw.get("protected_paths") or []:
        path, reason = _path_from_protected_item(item)
        if not path:
            continue
        if _creation_only_protection(path, reason):
            # protected_existing_* is specifically about mutation of files
            # already present. Creation restrictions are represented by the
            # absence of create_new operations and must not erase explicit
            # modify_existing authorization.
            continue
        if "*" in path:
            _append_unique(protected_existing_globs, path)
        else:
            _append_unique(protected_existing_paths, path)
            path_operations.append(
                {
                    "path": path,
                    "operation": "forbid_modify",
                    "evidence": reason[:400],
                    "source": "llm_write_scope_intent",
                }
            )

    issues: list[str] = []
    if bool(source_allowed_raw) and not bool(existing_allowed_raw):
        issues.append("source_modification_allowed_but_existing_file_modification_forbidden")
    if bool(source_allowed_raw) and ("**" in protected_existing_globs):
        issues.append("source_modification_allowed_but_all_existing_files_protected")
    if task_mode == "read_only_analysis" and (source_allowed_raw or create_paths or allowed_modify_paths):
        issues.append("read_only_task_mode_conflicts_with_write_operations")

    low_confidence = confidence < MIN_SOURCE_MODIFY_CONFIDENCE
    concrete_source_paths = bool(allowed_modify_paths) and task_mode in {"source_modify", "debug"}
    source_candidate = task_mode in {"source_modify", "debug"} or bool(source_allowed_raw) or concrete_source_paths
    source_effective = (
        (bool(source_allowed_raw) or concrete_source_paths)
        and bool(existing_allowed_raw)
        and "**" not in protected_existing_globs
    )
    safety_downgraded = False
    if source_candidate and (low_confidence or issues):
        source_effective = False
        safety_downgraded = True

    if task_mode in READ_ONLY_MODES or task_mode == "read_only_analysis":
        operation_mode = "read_only_analysis"
        mode = "analyze"
    elif source_effective:
        operation_mode = "scoped_modify"
        mode = "debug" if task_mode == "debug" else "modify"
    elif task_mode in CREATE_MODES or create_paths:
        operation_mode = "safe_create"
        mode = "write"
    elif task_mode in PROJECT_MODES:
        operation_mode = "safe_create"
        mode = "generate_project"
    elif source_candidate and safety_downgraded:
        operation_mode = "read_only_analysis"
        mode = "analyze"
    else:
        operation_mode = "read_only_analysis"
        mode = "analyze"

    ambiguities = raw.get("ambiguities") if isinstance(raw.get("ambiguities"), list) else []
    return {
        **base,
        "available": True,
        "valid": True,
        "source": "llm",
        "task_mode": task_mode,
        "operation_mode": operation_mode,
        "mode": mode,
        "confidence": confidence,
        "low_confidence": low_confidence,
        "source_modification_allowed": bool(source_effective),
        "existing_file_modification_allowed": bool(existing_allowed_raw),
        "safety_downgraded": safety_downgraded,
        "consistency_issues": issues,
        "allowed_modify_paths": allowed_modify_paths,
        "create_paths": create_paths,
        "read_reference_paths": read_reference_paths,
        "protected_existing_paths": protected_existing_paths,
        "protected_existing_globs": protected_existing_globs,
        "path_operations": path_operations,
        "ambiguities": [str(x)[:300] for x in ambiguities],
        "reason": str(raw.get("reason") or source_section.get("reason") or "")[:800],
    }


def merge_semantic_scope_contract(scope: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
    if not semantic.get("available") or not semantic.get("valid"):
        return scope
    out = dict(scope or {})
    out.setdefault("version", "scope_contract_v1")

    def extend_list(key: str, values: list[str]) -> None:
        current = [normalize_rel(p) for p in out.get(key) or [] if normalize_rel(p)]
        for value in values:
            rel = normalize_rel(value)
            if rel and rel not in current:
                current.append(rel)
        out[key] = current

    extend_list("allowed_modify_paths", semantic.get("allowed_modify_paths") or [])
    extend_list("protected_existing_paths", semantic.get("protected_existing_paths") or [])
    extend_list("forbidden_modify_paths", semantic.get("protected_existing_paths") or [])
    extend_list("protected_existing_globs", semantic.get("protected_existing_globs") or [])
    extend_list("read_reference_paths", semantic.get("read_reference_paths") or [])

    operations = list(out.get("path_operations") or [])
    seen = {(normalize_rel(item.get("path")), item.get("operation")) for item in operations if isinstance(item, dict)}
    for item in semantic.get("path_operations") or []:
        key = (normalize_rel(item.get("path")), item.get("operation"))
        if key[0] and key not in seen:
            operations.append(item)
            seen.add(key)
    out["path_operations"] = operations
    out["semantic_write_scope_source"] = semantic.get("source")
    return out
