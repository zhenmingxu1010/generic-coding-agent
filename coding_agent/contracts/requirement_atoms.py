from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from coding_agent.scope.write_scope_audit import build_write_scope_audit


REQUIREMENT_ATOM_VERSION = "v3.0"
UNVERIFIED_STATUSES = {"pending", "unverified", "unknown", ""}


def _norm_path(value: Any) -> str:
    text = str(value or "").strip().strip("'\"").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _is_symbolic_internal_test_path(value: Any) -> bool:
    parts = _norm_path(value).split("/")
    return (
        len(parts) >= 2
        and parts[0] == ".coding_agent_test"
        and parts[1].lower() in {
            "<thread-id>", "<thread_id>", "{thread-id}", "{thread_id}",
            "$thread-id", "$thread_id",
        }
    )


def _is_internal_test_location_policy(requirement: dict[str, Any], task: str) -> bool:
    """Return whether an LLM requirement only restates internal test placement.

    ``.coding_agent_test/<thread-id>`` is a runtime-owned symbolic location.
    A prompt that says agent-created tests *must live there* does not require
    the agent to create a test file.  Keep an actual test deliverable only when
    the user separately uses an explicit test-creation verb.
    """
    fields = [
        requirement.get("path"),
        requirement.get("description"),
        requirement.get("verification_hint"),
        *(requirement.get("user_evidence") or []),
        *(requirement.get("evidence") or []),
    ]
    joined = " ".join(str(value or "") for value in fields)
    if ".coding_agent_test" not in joined or not any(
        token in joined.lower()
        for token in ("<thread-id>", "<thread_id>", "{thread-id}", "{thread_id}", "$thread-id", "$thread_id")
    ):
        return False
    explicit_creation = bool(
        re.search(r"(?:create|add|generate|implement|write)\s+(?:an?\s+|new\s+|verification\s+|unit\s+|integration\s+)*tests?\b", task, re.IGNORECASE)
        or re.search(r"(?:创建|新增|添加|生成|编写).{0,16}(?:验证)?测试", task)
    )
    return not explicit_creation


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return text[:80] or "requirement"


def _make_atom(
    atom_id: str,
    atom_type: str,
    description: str,
    *,
    required: bool = True,
    source: str = "task",
    data: dict[str, Any] | None = None,
    evidence: list[str] | None = None,
    verify_hint: str = "",
) -> dict[str, Any]:
    return {
        "id": atom_id,
        "type": atom_type,
        "description": description,
        "required": bool(required),
        "status": "pending",
        "source": source,
        "evidence": list(evidence or []),
        "data": dict(data or {}),
        "verify_hint": verify_hint,
    }


def _intent(supervisor: dict[str, Any], task_spec: dict[str, Any]) -> dict[str, Any]:
    return dict(supervisor.get("task_intent") or task_spec.get("task_intent") or {})


def _create_paths(supervisor: dict[str, Any], task_spec: dict[str, Any]) -> list[str]:
    intent = _intent(supervisor, task_spec)
    values = list(intent.get("create_paths") or []) + list(task_spec.get("create_paths") or [])
    out: list[str] = []
    for value in values:
        rel = _norm_path(value)
        if (
            not rel
            or rel.startswith("/")
            or ".." in Path(rel).parts
            or "*" in rel
            or _is_symbolic_internal_test_path(rel)
        ):
            continue
        if rel not in out:
            out.append(rel)
    return out


def _resolve_artifact_path(path: str, create_paths: list[str]) -> str:
    """Reconcile an LLM artifact path with the task's concrete create targets.

    Intake may describe the same artifact with a shortened path while the
    resolved task intent contains its exact project-relative destination. A
    unique basename match is enough to identify that artifact; ambiguous
    matches are deliberately left unchanged.
    """
    rel = _norm_path(path)
    if not rel or rel in create_paths:
        return rel
    basename = Path(rel).name
    matches = [candidate for candidate in create_paths if Path(candidate).name == basename]
    return matches[0] if len(matches) == 1 else rel


def _structured_requirements(task_spec: dict[str, Any], supervisor: dict[str, Any]) -> list[dict[str, Any]]:
    raw = task_spec.get("requirements") or supervisor.get("requirements") or []
    if raw:
        return [
            dict(item)
            for item in raw
            if isinstance(item, dict)
            and str(item.get("scope") or "deliverable").lower() == "deliverable"
        ]
    return [
        {
            "id": f"criterion_{index + 1}",
            "kind": "behavior",
            "description": str(description),
            "required": True,
            "verification_hint": "Use observable execution evidence.",
        }
        for index, description in enumerate(task_spec.get("success_criteria") or [])
        if str(description).strip()
    ]


def _implementation_requirements(task_spec: dict[str, Any], supervisor: dict[str, Any]) -> list[dict[str, Any]]:
    raw = task_spec.get("implementation_requirements") or supervisor.get("implementation_requirements") or []
    return [dict(item) for item in raw if isinstance(item, dict)]


def extract_requirement_atoms(
    task: str,
    task_spec: dict[str, Any] | None = None,
    supervisor: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build a project-agnostic contract from LLM-structured requirements.

    The runtime only adds universal artifact and write-scope invariants. Domain,
    format, framework, and CLI semantics stay in task-specific requirements
    produced by the intake model.
    """
    task_spec = task_spec or {}
    supervisor = supervisor or {}
    intent = _intent(supervisor, task_spec)
    atoms: list[dict[str, Any]] = []
    create_paths = _create_paths(supervisor, task_spec)
    read_only_task = bool(
        intent.get("agent_read_only")
        or task_spec.get("read_only")
        or supervisor.get("read_only")
    )

    for path in create_paths:
        atoms.append(_make_atom(
            f"artifact:{path}",
            "artifact_exists",
            f"Required artifact exists: {path}",
            data={"path": path},
            evidence=[path],
            verify_hint="Check the requested project-relative artifact path.",
        ))

    operation_mode = str(intent.get("operation_mode") or supervisor.get("operation_mode") or "")
    if intent.get("agent_read_only") or task_spec.get("read_only") or supervisor.get("read_only"):
        atoms.append(_make_atom(
            "write_scope:agent_read_only",
            "write_scope",
            "The run must not write project files.",
            evidence=["resolved read-only intent"],
        ))
    semantic_scope = intent.get("semantic_write_scope") or {}
    explicit_existing_modification_ban = bool(
        semantic_scope.get("available")
        and semantic_scope.get("valid")
        and semantic_scope.get("existing_file_modification_allowed") is False
    )
    if not read_only_task and (
        operation_mode == "safe_create" or explicit_existing_modification_ban
    ):
        atoms.append(_make_atom(
            "write_scope:no_existing_project_modification",
            "write_scope",
            "Existing project files must not be modified.",
            evidence=["resolved safe-create scope"],
        ))

    for index, requirement in enumerate(_structured_requirements(task_spec, supervisor)):
        if (
            _is_symbolic_internal_test_path(requirement.get("path"))
            or _is_internal_test_location_policy(requirement, task)
        ):
            continue
        raw_id = str(requirement.get("id") or f"requirement_{index + 1}")
        kind = str(requirement.get("kind") or "behavior").lower()
        description = str(requirement.get("description") or raw_id).strip()
        path = _resolve_artifact_path(_norm_path(requirement.get("path")), create_paths)
        evidence_mode = str(
            requirement.get("evidence_mode")
            or (requirement.get("data") or {}).get("evidence_mode")
            or ("runtime" if kind == "constraint" else "execution")
        ).lower()
        if kind == "artifact" and not path:
            continue
        if evidence_mode == "analysis" and not read_only_task:
            continue
        if kind == "artifact" and path:
            atom_id = f"artifact:{path}"
            atom_type = "artifact_exists"
            data = {"path": path}
        else:
            atom_id = raw_id if ":" in raw_id else f"requirement:{_slug(raw_id)}"
            atom_type = kind if kind in {"behavior", "constraint", "quality"} else "behavior"
            data = dict(requirement.get("data") or {})
            data.setdefault("evidence_mode", evidence_mode)
        atoms.append(_make_atom(
            atom_id,
            atom_type,
            description,
            required=requirement.get("required", True),
            source="llm_task_requirement",
            data=data,
            evidence=[
                str(x)
                for x in (
                    list(requirement.get("user_evidence") or [])
                    + list(requirement.get("evidence") or [])
                )
                if str(x)
            ],
            verify_hint=str(requirement.get("verification_hint") or "Use observable execution evidence."),
        ))

    for index, requirement in enumerate(_implementation_requirements(task_spec, supervisor)):
        raw_id = str(requirement.get("id") or f"implementation_{index + 1}")
        description = str(requirement.get("description") or raw_id).strip()
        evidence_mode = str(requirement.get("evidence_mode") or "execution").lower()
        atoms.append(_make_atom(
            f"implementation:{_slug(raw_id)}",
            str(requirement.get("kind") or "behavior"),
            description,
            required=requirement.get("required", True),
            source="agent_implementation_default",
            data={"evidence_mode": evidence_mode, "contract_source": "agent_defaults"},
            evidence=["Agent implementation contract; not an explicit user requirement."],
            verify_hint=str(requirement.get("verification_hint") or "Use observable execution evidence."),
        ))

    deduped: list[dict[str, Any]] = []
    by_id: dict[str, int] = {}
    for atom in atoms:
        atom_id = str(atom["id"])
        if atom_id in by_id:
            existing = deduped[by_id[atom_id]]
            existing["required"] = bool(existing.get("required") or atom.get("required"))
            if atom.get("verify_hint") and not existing.get("verify_hint"):
                existing["verify_hint"] = atom["verify_hint"]
            continue
        by_id[atom_id] = len(deduped)
        deduped.append(atom)
    return deduped


def summarize_requirement_atoms(atoms: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    required_total = required_failed = required_unverified = 0
    for atom in atoms:
        status = str(atom.get("status") or "pending")
        status_counts[status] = status_counts.get(status, 0) + 1
        if atom.get("required", True):
            required_total += 1
            if status == "failed":
                required_failed += 1
            elif status in UNVERIFIED_STATUSES:
                required_unverified += 1
    return {
        "version": REQUIREMENT_ATOM_VERSION,
        "total": len(atoms),
        "required_total": required_total,
        "required_unverified": required_unverified,
        "required_failed": required_failed,
        "status_counts": status_counts,
    }


def _artifact_candidates(workspace: str, atom: dict[str, Any], state: dict[str, Any]) -> list[Path]:
    root = Path(workspace).resolve()
    rel = _norm_path((atom.get("data") or {}).get("path"))
    candidates = [rel] if rel else []
    for item in state.get("generated_files") or []:
        if not isinstance(item, dict):
            continue
        path = _norm_path(item.get("path"))
        original = _norm_path(item.get("original_path"))
        if rel and original == rel and path not in candidates:
            candidates.append(path)
    path_map = (state.get("output_layout") or {}).get("path_map") or {}
    mapped = _norm_path(path_map.get(rel))
    if mapped and mapped not in candidates:
        candidates.append(mapped)
    return [(root / candidate).resolve() for candidate in candidates if candidate]


def _verification_claims(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = state.get("verification_claims") or {}
    if isinstance(raw, list):
        return {str(item.get("atom_id")): item for item in raw if isinstance(item, dict) and item.get("atom_id")}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    return {}


def evaluate_requirement_atoms(
    workspace: str,
    atoms: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = state or {}
    root = Path(workspace).resolve()
    mode = str(state.get("mode") or "")
    claims = _verification_claims(state)
    evaluated: list[dict[str, Any]] = []
    failures: list[str] = []
    warnings: list[str] = []

    for atom in atoms:
        item = dict(atom)
        atom_id = str(item.get("id") or "")
        atom_type = str(item.get("type") or "behavior")
        details = dict(item.get("details") or {})

        if atom_type == "artifact_exists":
            candidates = _artifact_candidates(workspace, item, state)
            inside = [path for path in candidates if path == root or root in path.parents]
            exists = any(path.is_file() or path.is_dir() for path in inside)
            item["status"] = "passed" if exists else "failed"
            details["checked_paths"] = [str(path.relative_to(root)).replace("\\", "/") for path in inside]
            if not exists:
                failures.append(f"required artifact is missing for atom {atom_id}")
        elif atom_type == "write_scope":
            audit = build_write_scope_audit(state)
            if atom_id == "write_scope:agent_read_only":
                changed = bool(audit.get("all_recorded_changes") or state.get("changed_files"))
                item["status"] = "failed" if changed else "passed"
            else:
                item["status"] = "passed" if audit.get("no_existing_project_modification") else "failed"
            details["write_scope_audit"] = audit
            if item["status"] == "failed":
                failures.append(f"write-scope requirement failed for atom {atom_id}")
        elif mode == "analyze":
            item["status"] = "not_applicable"
            details["reason"] = "read-only analysis is validated by the analysis contract and report evidence"
        else:
            claim = claims.get(atom_id) or {}
            status = str(claim.get("status") or "unverified")
            if status not in {"passed", "failed", "unverified"}:
                status = "unverified"
            item["status"] = status
            details["verification_claim"] = claim
            if status == "failed":
                failures.append(f"required behavior failed for atom {atom_id}: {claim.get('reason') or item.get('description')}")
            elif status == "unverified":
                warnings.append(f"requirement atom {atom_id} has no sufficient execution evidence")

        item["details"] = details
        evaluated.append(item)

    summary = summarize_requirement_atoms(evaluated)
    return {
        "version": REQUIREMENT_ATOM_VERSION,
        "ok": not failures and summary["required_unverified"] == 0,
        "atoms": evaluated,
        "summary": summary,
        "failures": failures,
        "warnings": warnings,
    }
