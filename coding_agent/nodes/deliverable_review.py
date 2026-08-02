from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.core.utils import extract_json_object, sha16, truncate
from .common import get_trace


DELIVERABLE_REVIEW_SYSTEM = """You review the final user-visible files produced by a general coding agent.
Return JSON only:
{
  "blocking_issues": [
    {"path": "relative/file", "message": "concrete contradiction", "evidence": "short exact evidence"}
  ],
  "warnings": [
    {"path": "relative/file", "message": "non-blocking risk", "evidence": "short evidence"}
  ],
  "summary": "short result"
}

Rules:
- Compare only against the explicit task, task contract, and supplied file contents.
- Treat supplied reference contract files as authoritative. Check explicit input
  types, error behavior, examples, and public interfaces against changed files.
- Passing tests never waives an explicit contract clause that those tests may
  not cover. Do not claim a reference contract is satisfied merely because a
  supplied pytest summary is green.
- For every explicit input-type and invalid-input rule in a supplied contract,
  trace the changed implementation and require a concrete validation or an
  equivalent guaranteed behavior. A missing required type check is blocking.
- Audit container types and contained-value types separately. Iterating a
  value, accepting an empty value, or validating each iterated item does not
  by itself prove that the outer value has the required type.
- Keep contract subjects separate. A library function's exception contract
  applies when that function is called directly; a CLI may intentionally catch
  that exception and translate it into the CLI's documented stderr/exit-code
  behavior. Do not flag one public interface for correctly implementing its
  own clause merely because another interface has a different error contract.
- Trace multi-layer failure handling one boundary at a time. If a lower-level
  operation must convert a failure into an exception and an orchestrator must
  clean up or translate that exception, verify both layers independently. A
  higher layer manufacturing the same exception does not prove that the lower
  layer raises it, and letting the exception escape does not prove that the
  orchestrator stops in its required form.
- "Stop cleanly without propagating" means the failure path returns normally;
  a bare return/None is valid unless the task or a supplied contract explicitly
  requires another failure sentinel. Never apply the success-path return-value
  contract to a handled failure path without direct evidence.
- For newly introduced public symbols, inspect neighboring symbols and imports
  for the repository's established naming convention. A new exception, result,
  command, or callable whose public name contradicts a clear local convention
  is a compatibility risk. When the task gives an exact symbol name, any other
  spelling is blocking; otherwise report a convention mismatch as a warning
  unless supplied repository evidence makes the expected public name concrete.
- A blocking issue must be a concrete, localized contradiction that makes a requested deliverable incorrect or unusable, such as documentation disagreeing with an implemented command or an example's stated result contradicting its own input.
- Redundancy, dead code, an unused import, duplicate validation, or style debt is
  a warning unless it demonstrably changes required observable behavior. Never
  recommend removing behavior explicitly required by the task merely to remove
  a duplicate or unreachable branch; identify the incorrect layer instead.
- Do not invent requirements, private APIs, formatting preferences, dependencies, or speculative edge-case behavior.
- Treat a newly exposed command option, input mode, callable API, or public
  behavior as blocking when it has no basis in the explicit task, contract, or
  supplied project evidence. A generated verification command is not itself a
  requirement and cannot justify expanding the deliverable.
- Potential improvements and unstated edge cases are warnings, not blockers.
- Tests passing does not prove documentation examples are factually consistent.
- Use only supplied relative paths. Return empty lists when no grounded issue exists.
"""


CONTRACT_COUNTEREXAMPLE_SYSTEM = """You are the independent adversarial contract auditor for a general coding agent.
Return JSON only, using this schema:
{
  "blocking_issues": [
    {"path": "relative/editable/file", "message": "missing or contradictory behavior", "evidence": "contract clause plus exact code gap"}
  ],
  "warnings": [],
  "summary": "short result"
}

Attempt to falsify the claim that every explicit reference-contract clause is
implemented. Read the reference files sentence by sentence and construct a
minimal counterexample for each normative input, output, error, mutation, and
public-interface rule. Then trace the supplied implementation path.

Important distinctions:
- Validate an outer/container type independently from the types of its items.
- Empty or vacuously iterable wrong-type values are counterexamples unless the
  implementation explicitly rejects them or rejection is otherwise guaranteed.
- Validate success and failure exit codes, stdout, and stderr independently.
- Passing tests, task summaries, and intended behavior are not implementation evidence.

Emit a blocking issue only for a concrete contradiction supported by the
supplied contract and code. Point it at an editable supplied implementation
file. Do not invent requirements or suggest optional hardening.
"""


TASK_COUNTEREXAMPLE_SYSTEM = """You are the independent adversarial task auditor for a general coding agent.
Return JSON only with blocking_issues, warnings, and summary using the same
schema as the primary deliverable review.

Try to falsify every explicit required behavior from the task by tracing the
supplied implementation. Decompose each clause by its subject, trigger, action,
and observable result. Do not let one end-to-end success stand in for distinct
intermediate contracts.

For multi-layer failure handling:
- Locate the layer that directly observes the failure and verify that it
  performs the required exception or error conversion.
- Separately locate the caller responsible for cleanup, translation, rollback,
  or stopping, and verify its behavior.
- A caller manufacturing the same exception does not prove the lower layer
  raises it. An exception escaping the caller does not prove required cleanup
  or graceful translation.
- Preserve adjacent pre-existing return and exception behavior unless the task
  explicitly changes it.
- A handled failure that must stop cleanly without propagating may return None.
  Do not demand the normal success value on that failure path unless supplied
  evidence explicitly requires it.
- Inspect newly introduced public names against neighboring definitions and
  imports. Enforce an exact task-specified name. If no exact name is specified,
  distinguish a concrete repository convention mismatch from a merely
  subjective naming preference.

Emit only concrete contradictions against supplied editable files. Passing
tests and generated probes are not implementation evidence. Do not invent
requirements or implementation preferences.
"""


def _normalize_rel(path: Any) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _user_visible_changed_paths(state: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for source in (state.get("changed_files") or [], state.get("source_changed_files") or []):
        for value in source:
            rel = _normalize_rel(value)
            if not rel or rel.startswith((".coding_agent/", ".coding_agent_test/")):
                continue
            if rel not in out:
                out.append(rel)
    priority = {".md": 0, ".rst": 0, ".txt": 1, ".py": 2}
    if state.get("mode") in {"modify", "debug", "repair_existing"}:
        root = Path(str(state.get("workspace") or ".")).resolve()
        for value in (state.get("scope_contract") or {}).get("allowed_modify_paths") or []:
            rel = _normalize_rel(value)
            try:
                path = (root / rel).resolve()
                path.relative_to(root)
            except (OSError, ValueError):
                continue
            if rel and path.is_file() and rel not in out:
                out.append(rel)
    return sorted(out, key=lambda path: (priority.get(Path(path).suffix.lower(), 3), path))[:10]


def _collect_deliverables(state: dict[str, Any], max_chars: int = 20000) -> tuple[list[dict[str, Any]], str]:
    root = Path(str(state.get("workspace") or ".")).resolve()
    rows: list[dict[str, Any]] = []
    fingerprint_rows: list[dict[str, str]] = []
    remaining = max_chars
    for rel in _user_visible_changed_paths(state):
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            continue
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        fingerprint_rows.append({"path": rel, "sha16": sha16(raw)})
        if remaining <= 300:
            continue
        content = raw.decode("utf-8", errors="replace")
        preview = truncate(content, min(7000, remaining))
        rows.append({
            "path": rel,
            "sha16": sha16(raw),
            "content": preview,
            "truncated": len(preview) < len(content),
        })
        remaining -= len(preview)
    fingerprint = sha16(json.dumps(fingerprint_rows, ensure_ascii=False, sort_keys=True))
    return rows, fingerprint


def _collect_patch_evidence(state: dict[str, Any], max_chars: int = 10000) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    remaining = max_chars
    for record in state.get("repair_history") or []:
        if not isinstance(record, dict) or not record.get("diff_path") or remaining <= 200:
            continue
        try:
            path = Path(str(record["diff_path"]))
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        preview = truncate(content, min(3500, remaining))
        rows.append({
            "files": ", ".join(str(item) for item in record.get("files_changed") or []),
            "diff": preview,
        })
        remaining -= len(preview)
    return rows


def _collect_reference_contracts(
    state: dict[str, Any],
    *,
    excluded_paths: set[str],
    max_chars: int = 14000,
) -> list[dict[str, Any]]:
    root = Path(str(state.get("workspace") or ".")).resolve()
    candidates: list[str] = []
    for source in (
        (state.get("task_intent") or {}).get("read_reference_paths") or [],
        (state.get("scope_contract") or {}).get("read_reference_paths") or [],
    ):
        for value in source:
            rel = _normalize_rel(value)
            if rel and rel not in excluded_paths and rel not in candidates:
                candidates.append(rel)

    rows: list[dict[str, Any]] = []
    remaining = max_chars
    for rel in candidates[:10]:
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
            if not path.is_file() or remaining <= 300:
                continue
            raw = path.read_bytes()
        except (OSError, ValueError):
            continue
        content = raw.decode("utf-8", errors="replace")
        preview = truncate(content, min(7000, remaining))
        rows.append({
            "path": rel,
            "sha16": sha16(raw),
            "content": preview,
            "truncated": len(preview) < len(content),
            "role": "reference_contract",
        })
        remaining -= len(preview)
    return rows


def _direct_contract_verification_context(
    state: dict[str, Any],
    reference_contracts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose only direct contract-bound scenarios, not broad green suites."""
    contract_paths = {str(row.get("path") or "") for row in reference_contracts}
    steps = {
        str(step.get("name") or ""): step
        for step in (state.get("file_plan") or {}).get("verify_steps") or []
        if isinstance(step, dict)
        and any(
            _normalize_rel(citation.get("source")) in contract_paths
            for citation in step.get("basis") or []
            if isinstance(citation, dict)
        )
    }
    rows: list[dict[str, Any]] = []
    for result in (state.get("verification") or {}).get("results") or []:
        if not isinstance(result, dict):
            continue
        name = str(result.get("name") or "")
        step = steps.get(name)
        if not step:
            continue
        rows.append({
            "name": name,
            "expected": step.get("expected"),
            "normalized_returncode": result.get("returncode"),
            "actual_returncode": (
                result.get("actual_returncode")
                if result.get("actual_returncode") is not None
                else result.get("returncode")
            ),
            "success_exit_codes": result.get("success_exit_codes") or step.get("success_exit_codes") or [0],
            "stdout": truncate(str(result.get("stdout") or ""), 1000),
            "stderr": truncate(str(result.get("stderr") or ""), 1000),
            "timed_out": bool(result.get("timed_out")),
        })
    return rows[:12]


def _review_task_contract(state: dict[str, Any]) -> dict[str, Any]:
    """Remove model-authored verification suggestions from review authority."""
    contract = state.get("task_contract") or {}
    atoms = []
    for atom in contract.get("requirement_atoms") or []:
        if not isinstance(atom, dict):
            continue
        atoms.append({
            "id": atom.get("id"),
            "type": atom.get("type"),
            "description": atom.get("description"),
            "required": atom.get("required", True),
            "evidence": atom.get("evidence") or [],
        })
    return {
        "objective": contract.get("objective"),
        "constraints": contract.get("constraints") or [],
        "prohibited_actions": contract.get("prohibited_actions") or [],
        "requirement_atoms": atoms,
    }


def deliverable_review_needed(state: dict[str, Any]) -> bool:
    if state.get("read_only") or state.get("mode") in {"analyze", "run_verify"}:
        return False
    rows, fingerprint = _collect_deliverables(state, max_chars=1000)
    if not rows:
        return False
    return fingerprint != str(state.get("deliverable_review_fingerprint") or "")


def _valid_issue_rows(value: Any, allowed_paths: set[str], *, limit: int = 8) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        path = _normalize_rel(item.get("path"))
        message = str(item.get("message") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if path not in allowed_paths or not message or not evidence:
            continue
        normalized_evidence = " ".join(evidence.lower().split()).rstrip(".!")
        normalized_message = " ".join(message.lower().split()).rstrip(".!")

        def denies_blocking_issue(text: str) -> bool:
            return bool(
                "no contradiction" in text
                or "not a contradiction" in text
                or "false positive" in text
                or (
                    "no blocking issue" in text
                    and any(marker in text for marker in ("found", "exists", "identified", "present"))
                )
                or re.search(
                    r"\b(?:but|however)\b.{0,180}\b"
                    r"(?:required\s+)?(?:phrase|behavior|check|guard|symbol|value)"
                    r"\b.{0,100}\b(?:is|are)\s+present\b",
                    text,
                )
                is not None
            )

        if normalized_evidence in {
            "no contradiction found",
            "no blocking issue",
            "no blocking issues found",
            "none",
        }:
            continue
        if normalized_evidence.endswith((
            "no contradiction found",
            "no blocking issue",
            "no blocking issues found",
            "this is not a contradiction",
        )):
            continue
        if denies_blocking_issue(normalized_evidence):
            continue
        if normalized_message.endswith((
            "no contradiction found",
            "no blocking issue",
            "no blocking issues found",
            "this is not a contradiction",
        )):
            continue
        if denies_blocking_issue(normalized_message):
            continue
        out.append({
            "path": path,
            "message": message[:1200],
            "evidence": evidence[:1200],
        })
        if len(out) >= limit:
            break
    return out


def _counterexample_contract_review(
    client: OpenAICompatClient,
    *,
    reference_contracts: list[dict[str, Any]],
    files: list[dict[str, Any]],
    allowed_paths: set[str],
    direct_verification: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    """Run an independent falsification pass after an apparently clean audit."""
    prompt = (
        "Referenced contract files (authoritative):\n"
        f"{truncate(json.dumps(reference_contracts, ensure_ascii=False), 15000)}\n\n"
        "Direct contract-bound execution evidence (actual exit code is authoritative):\n"
        f"{truncate(json.dumps(direct_verification, ensure_ascii=False), 7000)}\n\n"
        "Editable implementation files:\n"
        f"{truncate(json.dumps(files, ensure_ascii=False), 22000)}"
    )
    text = client.chat(
        [
            {"role": "system", "content": CONTRACT_COUNTEREXAMPLE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        purpose="deliverable_contract_counterexample",
        max_tokens=1400,
    )
    raw = extract_json_object(text)
    return (
        _valid_issue_rows(raw.get("blocking_issues"), allowed_paths),
        _valid_issue_rows(raw.get("warnings"), allowed_paths),
        str(raw.get("summary") or "")[:1200],
    )


def _task_counterexample_review_needed(
    state: dict[str, Any],
    files: list[dict[str, Any]],
) -> bool:
    python_files = [row for row in files if Path(str(row.get("path") or "")).suffix == ".py"]
    behavior_atoms = [
        atom
        for atom in (state.get("task_contract") or {}).get("requirement_atoms") or []
        if isinstance(atom, dict)
        and atom.get("required", True)
        and str(atom.get("type") or "") == "behavior"
    ]
    return len(python_files) >= 2 and len(behavior_atoms) >= 2


def _counterexample_task_review(
    client: OpenAICompatClient,
    *,
    state: dict[str, Any],
    files: list[dict[str, Any]],
    allowed_paths: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]], str]:
    prompt = (
        f"Task:\n{state.get('task')}\n\n"
        "Explicit task contract:\n"
        f"{truncate(json.dumps(_review_task_contract(state), ensure_ascii=False), 7000)}\n\n"
        "Actual change diffs:\n"
        f"{truncate(json.dumps(_collect_patch_evidence(state), ensure_ascii=False), 10000)}\n\n"
        "Editable implementation files, including authorized unchanged files:\n"
        f"{truncate(json.dumps(files, ensure_ascii=False), 26000)}"
    )
    text = client.chat(
        [
            {"role": "system", "content": TASK_COUNTEREXAMPLE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        purpose="deliverable_task_counterexample",
        max_tokens=1400,
    )
    raw = extract_json_object(text)
    return (
        _valid_issue_rows(raw.get("blocking_issues"), allowed_paths),
        _valid_issue_rows(raw.get("warnings"), allowed_paths),
        str(raw.get("summary") or "")[:1200],
    )


def deliverable_review_node(state: dict[str, Any]) -> dict[str, Any]:
    trace = get_trace(state)
    files, fingerprint = _collect_deliverables(state)
    trace.event("deliverable_review_start", files=[row["path"] for row in files], fingerprint=fingerprint)
    if not files:
        state["deliverable_review"] = {"ok": True, "skipped": True, "reason": "no user-visible changed files"}
        state["deliverable_review_fingerprint"] = fingerprint
        trace.event("deliverable_review_done", review=state["deliverable_review"])
        return state

    allowed_paths = {row["path"] for row in files}
    reference_contracts = _collect_reference_contracts(
        state,
        excluded_paths=allowed_paths,
    )
    direct_verification = _direct_contract_verification_context(state, reference_contracts)

    if reference_contracts:
        verification_context = (
            "Broad suite summaries omitted. Direct contract-bound scenarios:\n"
            + truncate(json.dumps(direct_verification, ensure_ascii=False), 7000)
        )
        grounding_context = "Omitted to prevent verification-plan claims from overriding source contracts."
        mandatory_audit = (
            "Mandatory contract audit:\n"
            "1. Inspect every MUST/required/input-type/invalid-input clause in each reference file.\n"
            "2. Identify the exact supplied implementation code that enforces each clause.\n"
            "3. If an enforcement path is absent or admits a contradictory input, emit a blocking issue "
            "against an editable supplied source file.\n"
            "4. Do not use test results or verification hints to fill an implementation gap.\n\n"
        )
    else:
        verification_context = truncate(json.dumps(state.get("verification"), ensure_ascii=False), 3500)
        grounding_context = truncate(json.dumps(state.get("verification_oracle_review"), ensure_ascii=False), 3000)
        mandatory_audit = ""

    prompt = (
        f"Task:\n{state.get('task')}\n\n"
        f"Task contract (verification hints removed):\n"
        f"{truncate(json.dumps(_review_task_contract(state), ensure_ascii=False), 5000)}\n\n"
        f"{mandatory_audit}"
        f"Verification context:\n{verification_context}\n\n"
        f"Verification grounding context:\n{grounding_context}\n\n"
        f"Actual change diffs:\n{truncate(json.dumps(_collect_patch_evidence(state), ensure_ascii=False), 10000)}\n\n"
        f"Referenced contract files (read-only):\n"
        f"{truncate(json.dumps(reference_contracts, ensure_ascii=False), 15000)}\n\n"
        f"User-visible changed files:\n{truncate(json.dumps(files, ensure_ascii=False), 22000)}"
    )
    state["deliverable_review_prompt_chars"] = len(prompt)
    try:
        client = OpenAICompatClient("configs/model.yaml", messages_path=state.get("messages_path"))
        text = client.chat(
            [
                {"role": "system", "content": DELIVERABLE_REVIEW_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            purpose="deliverable_review",
            max_tokens=1200,
        )
        raw = extract_json_object(text)
        blocking = _valid_issue_rows(raw.get("blocking_issues"), allowed_paths)
        warnings = _valid_issue_rows(raw.get("warnings"), allowed_paths)
        summary = str(raw.get("summary") or "")[:1200]
        if reference_contracts:
            counterexample_blocking, counterexample_warnings, counterexample_summary = (
                _counterexample_contract_review(
                    client,
                    reference_contracts=reference_contracts,
                    files=files,
                    allowed_paths=allowed_paths,
                    direct_verification=direct_verification,
                )
            )
            # Put the focused falsification findings first. The broader review
            # can occasionally identify a real target for the wrong reason;
            # keeping both preserves evidence while giving repair the more
            # contract-specific diagnosis as its primary issue.
            blocking = counterexample_blocking + blocking
            warnings.extend(counterexample_warnings)
            if counterexample_blocking or counterexample_warnings:
                summary = counterexample_summary or summary
        elif not blocking and _task_counterexample_review_needed(state, files):
            counterexample_blocking, counterexample_warnings, counterexample_summary = (
                _counterexample_task_review(
                    client,
                    state=state,
                    files=files,
                    allowed_paths=allowed_paths,
                )
            )
            blocking = counterexample_blocking + blocking
            warnings.extend(counterexample_warnings)
            if counterexample_blocking or counterexample_warnings:
                summary = counterexample_summary or summary
    except Exception as exc:
        blocking = []
        warnings = [{
            "path": files[0]["path"],
            "message": "final deliverable review could not be completed",
            "evidence": str(exc)[:1000],
        }]
        summary = "deliverable review failed open because execution verification remains authoritative"
        state.setdefault("deliverable_review_errors", []).append(str(exc)[:2000])

    state["deliverable_review_fingerprint"] = fingerprint
    state["deliverable_review_count"] = int(state.get("deliverable_review_count", 0) or 0) + 1
    state["deliverable_review"] = {
        "ok": not blocking,
        "blocking_issues": blocking,
        "warnings": warnings,
        "summary": summary,
        "reviewed_files": sorted(allowed_paths),
        "reviewed_reference_files": sorted(row["path"] for row in reference_contracts),
        "fingerprint": fingerprint,
    }
    if warnings:
        state.setdefault("quality_warnings", []).extend(
            f"deliverable review: {item['path']}: {item['message']}"
            for item in warnings
        )
    if blocking:
        issues = [
            {
                "owner": "implementation",
                "type": "deliverable_consistency_error",
                "file": item["path"],
                "target_file": item["path"],
                "message": item["message"],
                "evidence": item["evidence"],
                "source": "deliverable_review",
            }
            for item in blocking
        ]
        state["failure_issues"] = issues
        state["failure"] = {
            "failure_type": "deliverable_consistency_error",
            "priority": 6,
            "message": "; ".join(item["message"] for item in blocking)[:2000],
            "target_file": blocking[0]["path"],
            "signature": "deliverable:" + sha16(json.dumps(blocking, ensure_ascii=False, sort_keys=True)),
            "raw_excerpt": json.dumps(blocking, ensure_ascii=False)[:4000],
            "source": "deliverable_review",
        }
        state["needs_verification"] = False
        state.pop("stopped_reason", None)
    trace.event("deliverable_review_done", review=state["deliverable_review"], failure=state.get("failure"))
    trace.snapshot(state)
    return state


def route_after_deliverable_review(state: dict[str, Any]) -> str:
    if (state.get("deliverable_review") or {}).get("blocking_issues"):
        if int(state.get("round_idx", 0) or 0) >= int(state.get("max_rounds", 12) or 12):
            state["stopped_reason"] = "max_rounds"
            return "report"
        return "repair"
    if (state.get("verification") or {}).get("ok") is not True:
        atom_summary = state.get("requirement_atom_summary") or {}
        unverified = int(atom_summary.get("required_unverified", 0) or 0)
        state["stopped_reason"] = "verification_evidence_incomplete"
        state["needs_verification"] = False
        state["failure"] = {
            "failure_type": "verification_evidence_incomplete",
            "priority": 6,
            "message": (
                f"{unverified} required behavior(s) remain unverified after deliverable audit"
                if unverified
                else "execution verification remains unsuccessful after deliverable audit"
            ),
            "target_file": None,
            "signature": "verification_evidence_incomplete",
            "raw_excerpt": str(state.get("verification_claims") or {})[:4000],
            "source": "route_after_deliverable_review",
        }
        return "report"
    state["stopped_reason"] = state.get("stopped_reason") or "verified_ok"
    state["failure"] = None
    state["needs_verification"] = False
    return "report"
