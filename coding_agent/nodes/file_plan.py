from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.core.schemas import FilePlan
from coding_agent.verification.test_registry import refresh_verification_test_registry
from coding_agent.scope.write_scope import extract_mentioned_paths, build_write_scope_policy
from coding_agent.scope.write_guard import path_has_explicit_create_intent, is_probably_code_output
from coding_agent.scope.write_intent import build_write_intents
from coding_agent.scope.task_intent import classify_task_intent
from coding_agent.contracts.artifact_constraints import is_prohibited_artifact_path
from coding_agent.scope.scope_contract import protected_original_output
from coding_agent.workspace.run_paths import apply_output_layout, is_test_like_path, is_under_test_support_dir
from coding_agent.core.utils import extract_json_object, truncate
from coding_agent.verification.plan_grounding import validate_step_grounding
from .common import get_trace


FILE_PLAN_SYSTEM = """You are the FilePlan node of a general Coding Agent.
You create a small, concrete file plan for writing code in an empty or partially empty workspace.
Return JSON only. Do not write prose.

Schema:
{
  "files": [
    {"path": "relative/path.py", "purpose": "why this file is needed", "kind": "code|test|readme|config|data|other"}
  ],
  "verify_steps": [
    {
      "name": "short_unique_name",
      "command": ["python", "relative/script.py", "--example"],
      "verifies": ["requirement:exact_requirement_id"],
      "basis": [
        {"source": "task|exact_requirement_id|repository/relative/path", "quote": "exact supporting excerpt"}
      ],
      "expected": "observable result supported by the cited excerpt",
      "timeout_sec": 180,
      "success_exit_codes": [0],
      "stdin": "optional text sent to the process standard input",
      "sandbox": {
        "copy_paths": ["relative/file.py", "fixtures/*.data"],
        "files": [{"path": "relative/input.data", "content": "temporary fixture"}],
        "omit_paths": ["relative/path_that_must_be_absent"]
      }
    }
  ],
  "rationale": "short reason"
}

Rules:
- Do not hard-code one project type. Choose files from the task contract and workspace state.
- Work inside the workspace only. Use relative paths. No /tmp. No absolute paths.
- For a script/tool task, include at least one code file. Include tests if the contract requests tests; the runtime may store those generated verification tests outside the delivered project files.
- For a small project task, include README, entrypoint, core logic, and tests only when requested by the contract or user task.
- Do not add user-facing test files just because verification is useful. Internal verification can run commands without making tests a delivered artifact.
- Never include files whose artifact kind is prohibited by the task contract.
- Keep the plan minimal and verifiable. Avoid unnecessary packages.
- Prefer standard library unless the task requires external packages.
- Include direct, non-shell verification steps appropriate to the task.
- Every required execution-evidence behavior/quality requirement must be
  covered by a step that exercises observable behavior. Runtime-evidence
  constraints such as write scope and artifact placement are checked by the
  agent runtime; do not add VCS or shell commands merely to prove them.
- `--help` and compile-only checks prove syntax/interface availability, not
  functional behavior.
- Use the exact requirement IDs supplied in the task contract.
- Every verification step must bind `verifies` to at least one exact requirement ID.
- Every verification step must include `basis` with an exact quote from the
  user task, a bound requirement, or supplied repository evidence. Cite the
  repository-relative path as `source` when using project evidence.
- `expected` must state the observable result justified by that basis.
- Do not add command options, input modes, public APIs, or behavior dimensions
  that are absent from the cited evidence.
- A public command option must be quoted from the user task or repository
  evidence. An LLM-generated requirement description alone cannot authorize a
  new option.
- Repository source may establish invocation syntax for requested behavior; it
  does not make unrelated implementation behavior a requirement.
- Verification outputs may use `{verification_dir}/name.ext`; the runtime
  expands that placeholder to an internal, non-deliverable directory.
- When a behavior cannot be exercised safely in the real workspace, use an
  optional sandbox. The runtime creates an isolated directory under its test
  area, copies only copy_paths, creates declared fixture files at their exact
  sandbox-relative paths, applies omit_paths, and runs the command from the
  sandbox root. Commands must reference declared fixture files by those exact
  paths. `{verification_dir}` is for generated outputs, never fixture inputs.
  Do not mutate real project inputs.
- `success_exit_codes` defaults to `[0]`. Include a documented nonzero code
  only when that exact exit status is the required successful observation.
- Do not use shell redirection, inline shell commands, `python -c`, or external
  paths. A shell script may be invoked only as `sh relative/script.sh ...` or
  `bash relative/script.sh ...` with no interpreter options.
- To exercise a program that reads standard input, set the step's `stdin`
  string. Do not create an unused fixture file and assume it will be piped.
"""


ABS_PATH_TOKEN_RE = re.compile(r"^(/[^\s]+|[A-Za-z]:[\\/].+)")
SHELL_META_RE = re.compile(r"(&&|\|\||[<>|;])")


def _workspace_has_files(repo_map: dict[str, Any]) -> bool:
    return bool((repo_map or {}).get("files"))


def _is_test_file(path: str) -> bool:
    return is_test_like_path(path)


def _test_equivalence_key(path: str) -> str | None:
    rel = str(path or "").replace("\\", "/")
    if not _is_test_file(rel):
        return None
    name = Path(rel).name
    stem = Path(name).stem
    if stem.startswith("test_"):
        stem = stem[5:]
    elif stem.endswith("_test"):
        stem = stem[:-5]
    return stem or name


def _prefer_test_path(item: dict[str, Any]) -> tuple[int, int, str]:
    path = str(item.get("path") or "").replace("\\", "/")
    original = str(item.get("original_path") or "").replace("\\", "/")
    in_tests_dir = is_under_test_support_dir(path) or is_under_test_support_dir(original)
    explicit = bool(item.get("original_path"))
    return (1 if in_tests_dir else 0, 1 if explicit else 0, -len(path))


def _dedupe_equivalent_test_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid generating both test_x.py and tests/test_x.py for the same target."""
    best_by_key: dict[str, dict[str, Any]] = {}
    other: list[dict[str, Any]] = []
    for item in files:
        key = _test_equivalence_key(str(item.get("path") or ""))
        if not key:
            other.append(item)
            continue
        current = best_by_key.get(key)
        if current is None or _prefer_test_path(item) > _prefer_test_path(current):
            best_by_key[key] = item
    test_items = sorted(best_by_key.values(), key=lambda x: str(x.get("path") or ""))
    return other + test_items


def _filter_prohibited_files(files: list[dict[str, Any]], prohibited_artifacts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in files:
        path = str(item.get("path") or "").replace("\\", "/")
        if is_prohibited_artifact_path(path, prohibited_artifacts):
            blocked.append(item)
        else:
            kept.append(item)
    return kept, blocked


def _filter_scope_protected_files(files: list[dict[str, Any]], scope_contract: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in files:
        path = str(item.get("path") or "").replace("\\", "/")
        original = str(item.get("original_path") or "").replace("\\", "/")
        if protected_original_output(scope_contract, path, original_path=original):
            blocked.append(item)
        else:
            kept.append(item)
    return kept, blocked


def _required_create_targets(task: str, contract: dict[str, Any], task_intent: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return contract-required output files.

    TaskIntentResolver is the source for create targets. This avoids
    converting read/input references such as experiments/*summary.json into
    outputs while still allowing prompts like "create a read-only analysis
    script" to create the script.
    """
    intent = task_intent or classify_task_intent(task or "", {"expected_artifacts": (contract or {}).get("expected_artifacts", [])})
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for p in intent.get("create_paths", []) or []:
        p = str(p).replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        if not p or p.startswith("/") or ".." in Path(p).parts:
            continue
        if p.startswith(".coding_agent/"):
            continue
        kind = "test" if p.startswith("tests/") or Path(p).name.startswith("test_") else ("readme" if p.lower().endswith(".md") else ("code" if p.endswith(".py") else "other"))
        if p not in seen:
            purpose = "pytest/smoke tests" if kind == "test" else "required output path from user task"
            out.append({
                "path": p,
                "purpose": purpose,
                "kind": kind,
                "explicit_user_requested": True,
                "contract_required": True,
            })
            seen.add(p)
    return out


def _fallback_file_plan(state: dict, reason: str) -> dict[str, Any]:
    """Return an empty plan when the LLM fails to plan.

    The runtime must not invent project structure. Explicit user-mentioned
    create targets are added later by `_required_create_targets`; otherwise a
    missing file plan is surfaced as a real planning failure.
    """
    return {"files": [], "verify_steps": [], "rationale": f"llm_file_plan_failed: {reason}"}


def _absolute_path_candidates(token: str) -> list[str]:
    text = str(token or "").strip().strip("'\"")
    candidates = [text]
    if "=" in text:
        _key, value = text.split("=", 1)
        if value:
            candidates.append(value.strip().strip("'\""))
    return [item for item in candidates if item]


def _token_is_external_absolute_path(token: str, *, workspace: Path, run_dir: Path | None) -> bool:
    candidates = _absolute_path_candidates(token)
    if not any(ABS_PATH_TOKEN_RE.match(item) for item in candidates):
        return False
    try:
        roots = [workspace.resolve()]
        if run_dir:
            roots.append(run_dir.resolve())
        for item in candidates:
            if not ABS_PATH_TOKEN_RE.match(item):
                continue
            candidate = Path(item).resolve()
            inside_known_root = False
            for root in roots:
                try:
                    candidate.relative_to(root)
                    inside_known_root = True
                    break
                except ValueError:
                    continue
            if not inside_known_root:
                return True
        return False
    except Exception:
        return True


def _sanitize_verify_steps(
    state: dict[str, Any],
    steps: list[dict[str, Any]] | None,
    *,
    planned_files: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate task-specific verification steps without interpreting their domain."""
    workspace = Path(state["workspace"]).resolve()
    run_dir = Path(state["run_dir"]).resolve() if state.get("run_dir") else None
    known_atoms = {
        str(atom.get("id"))
        for atom in (state.get("task_contract") or {}).get("requirement_atoms") or []
        if isinstance(atom, dict) and atom.get("id")
    }
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    known_project_files = {
        str(item.get("path") or "").strip().replace("\\", "/")
        for item in planned_files or []
        if isinstance(item, dict) and item.get("path")
    }
    known_project_files.update(
        str(path).replace("\\", "/")
        for path in (state.get("repo_map") or {}).get("files") or []
        if path
    )
    for index, raw in enumerate(steps or []):
        if not isinstance(raw, dict):
            skipped.append({"step": raw, "reason": "verification step is not an object"})
            continue
        cmd = [str(part) for part in raw.get("command") or [] if str(part)]
        if not cmd:
            continue
        name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(raw.get("name") or f"step_{index + 1}"))[:80]
        if not name or name in seen_names:
            skipped.append({"step": raw, "reason": "verification step name is empty or duplicated"})
            continue
        if name in {"py_compile", "pytest"}:
            skipped.append({"step": raw, "reason": "verification step name is reserved for a runtime infrastructure check"})
            continue
        joined = " ".join(cmd)
        if SHELL_META_RE.search(joined):
            skipped.append({"step": raw, "reason": "verification step uses shell syntax"})
            continue
        exe = Path(cmd[0]).name.lower()
        if exe in {"cmd", "cmd.exe", "powershell", "pwsh"}:
            skipped.append({"step": raw, "reason": "verification step may not invoke a shell interpreter"})
            continue
        if exe in {"bash", "sh"} and (
            len(cmd) < 2
            or cmd[1].startswith("-")
            or Path(cmd[1]).suffix.lower() not in {".sh", ".bash"}
        ):
            skipped.append({"step": raw, "reason": "verification step may not invoke a shell interpreter"})
            continue
        if exe in {"python", "python.exe", "python3", "python3.exe"} and len(cmd) > 1 and cmd[1] in {"-c", "-"}:
            skipped.append({"step": raw, "reason": "verification step may not execute inline Python"})
            continue
        external = [
            part
            for index, part in enumerate(cmd)
            if index > 0
            and "{verification_dir}" not in part
            and _token_is_external_absolute_path(part, workspace=workspace, run_dir=run_dir)
        ]
        if external:
            skipped.append({"step": raw, "reason": "verification step uses absolute path outside workspace/run directory", "paths": external})
            continue
        verifies = [str(atom_id) for atom_id in raw.get("verifies") or [] if str(atom_id) in known_atoms]
        if not verifies:
            skipped.append({"step": raw, "reason": "verification step is not bound to a known requirement"})
            continue
        fixture_corrections: list[dict[str, str]] = []
        sandbox = _sanitize_verification_sandbox(
            raw.get("sandbox"),
            fixture_corrections=fixture_corrections,
        )
        if sandbox:
            inferred_copy_paths = _sandbox_command_project_inputs(cmd, known_project_files)
            for rel in inferred_copy_paths:
                if rel not in sandbox["copy_paths"]:
                    sandbox["copy_paths"].append(rel)
            if inferred_copy_paths:
                state.setdefault("inferred_sandbox_copy_paths", []).append({
                    "name": name,
                    "paths": inferred_copy_paths,
                })
            cmd, command_corrections = _normalize_sandbox_fixture_references(cmd, sandbox)
            corrections = fixture_corrections + command_corrections
            if corrections:
                state.setdefault("normalized_file_plan_verify_steps", []).append({
                    "name": name,
                    "corrections": corrections,
                })
        item = {
            "name": name,
            "command": cmd,
            "verifies": list(dict.fromkeys(verifies)),
            "basis": list(raw.get("basis") or []),
            "expected": str(raw.get("expected") or "").strip(),
            "timeout_sec": max(5, min(int(raw.get("timeout_sec", 180) or 180), 300)),
            "success_exit_codes": _sanitize_success_exit_codes(raw.get("success_exit_codes")),
        }
        if raw.get("stdin") is not None:
            item["stdin"] = str(raw.get("stdin"))[:50000]
        if sandbox:
            item["sandbox"] = sandbox
        grounding = validate_step_grounding(state, item)
        state.setdefault("verification_grounding", {})[name] = grounding
        if grounding["status"] != "accepted":
            skipped.append({
                "step": raw,
                "reason": "verification step is not grounded in task or project evidence",
                "grounding": grounding,
            })
            continue
        item["basis"] = grounding["citations"]
        item["expected"] = grounding["expected"]
        item["grounding"] = grounding
        kept.append(item)
        seen_names.add(name)
    if skipped:
        state.setdefault("skipped_file_plan_verify_steps", []).extend(skipped)
    return kept


def _safe_sandbox_rel(value: Any, *, allow_glob: bool = False) -> str:
    rel = str(value or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
        return ""
    if "{verification_dir}" in rel:
        return ""
    if ".." in Path(rel).parts:
        return ""
    if not allow_glob and any(token in rel for token in ("*", "?", "[")):
        return ""
    return rel


def _sanitize_success_exit_codes(value: Any) -> list[int]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[int] = []
    for item in values:
        if isinstance(item, bool):
            continue
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= code <= 255 and code not in out:
            out.append(code)
    return out[:8] or [0]


def _sandbox_fixture_path(value: Any) -> tuple[str, dict[str, str] | None]:
    raw_path = str(value or "").strip().replace("\\", "/")
    normalized = raw_path
    for prefix in ("{verification_dir}/", ".verification/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    rel = _safe_sandbox_rel(normalized)
    correction = {"from": raw_path, "to": rel} if rel and rel != raw_path else None
    return rel, correction


def _sanitize_verification_sandbox(
    raw: Any,
    *,
    fixture_corrections: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    copy_paths = list(dict.fromkeys(
        rel
        for value in raw.get("copy_paths") or []
        if (rel := _safe_sandbox_rel(value, allow_glob=True))
    ))[:40]
    omit_paths = list(dict.fromkeys(
        rel
        for value in raw.get("omit_paths") or []
        if (rel := _safe_sandbox_rel(value))
    ))[:40]
    files: list[dict[str, str]] = []
    for item in raw.get("files") or []:
        if not isinstance(item, dict):
            continue
        rel, correction = _sandbox_fixture_path(item.get("path"))
        if not rel:
            continue
        if correction is not None and fixture_corrections is not None:
            fixture_corrections.append(correction)
        files.append({"path": rel, "content": str(item.get("content") or "")[:50000]})
        if len(files) >= 20:
            break
    if not copy_paths and not omit_paths and not files:
        return None
    return {"copy_paths": copy_paths, "files": files, "omit_paths": omit_paths}


def _normalize_sandbox_fixture_references(
    command: list[str],
    sandbox: dict[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    """Make command paths agree with declared sandbox fixture locations.

    This is a structural correction only: it applies when the suffix exactly
    identifies a declared fixture. Output paths and ambiguous paths are left
    untouched.
    """
    fixture_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in sandbox.get("files") or []
        if isinstance(item, dict) and item.get("path")
    }
    if not fixture_paths:
        return list(command), []

    normalized: list[str] = []
    corrections: list[dict[str, str]] = []
    prefixes = ("{verification_dir}/", ".verification/")
    for raw_token in command:
        token = str(raw_token)
        option_prefix = ""
        path_token = token
        if "=" in token:
            option_prefix, path_token = token.split("=", 1)
            option_prefix += "="
        comparable = path_token.replace("\\", "/")
        replacement = ""
        for prefix in prefixes:
            if comparable.startswith(prefix):
                candidate = comparable[len(prefix):]
                if candidate in fixture_paths:
                    replacement = option_prefix + candidate
                    break
        if replacement:
            normalized.append(replacement)
            corrections.append({"from": token, "to": replacement})
        else:
            normalized.append(token)
    return normalized, corrections


def _sandbox_command_project_inputs(command: list[str], known_project_files: set[str]) -> list[str]:
    """Infer sandbox copies for concrete project files referenced by argv."""
    inferred: list[str] = []
    executable = Path(str(command[0])).name.lower() if command else ""
    if (
        executable in {"python", "python.exe", "python3", "python3.exe"}
        and len(command) >= 3
        and command[1] == "-m"
        and command[2] != "pytest"
    ):
        module = str(command[2]).strip().replace("-", "_")
        module_path = module.replace(".", "/")
        top_level = module_path.split("/", 1)[0]
        module_file = f"{module_path}.py"
        if any(path == f"{top_level}.py" for path in known_project_files):
            inferred.append(f"{top_level}.py")
        elif any(path.startswith(f"{top_level}/") for path in known_project_files):
            # Copy the whole import package so relative and sibling imports
            # keep working in the disposable verification workspace.
            inferred.append(top_level)
        elif module_file in known_project_files:
            inferred.append(module_file)
    for token in command[1:]:
        value = str(token)
        if "=" in value:
            _option, value = value.split("=", 1)
        if value.startswith("-") or "{verification_dir}" in value:
            continue
        rel = _safe_sandbox_rel(value)
        if rel in known_project_files and rel not in inferred:
            inferred.append(rel)
    return inferred


def _planning_evidence(state: dict[str, Any], max_chars: int = 12000) -> str:
    pack = state.get("context_pack") or {}
    blocks = [block for block in pack.get("evidence_blocks") or [] if isinstance(block, dict)]
    read_refs = {
        str(path).replace("\\", "/")
        for path in (state.get("task_intent") or {}).get("read_reference_paths") or []
    }
    blocks.sort(
        key=lambda block: (
            str(block.get("path") or "").replace("\\", "/") in read_refs,
            int(block.get("priority", 0) or 0),
        ),
        reverse=True,
    )
    parts: list[str] = []
    used = 0
    for block in blocks:
        path = str(block.get("path") or "")
        content = str(block.get("content") or "")
        if not path or not content:
            continue
        piece = f"### {path}\n{content}\n"
        remaining = max_chars - used
        if remaining <= 300:
            break
        piece = truncate(piece, remaining)
        parts.append(piece)
        used += len(piece)
    return "\n".join(parts)


def file_plan_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("file_plan_start", mode=state.get("mode"), repo_files=len((state.get("repo_map") or {}).get("files", [])))
    if state.get("write_locked") or state.get("read_only"):
        plan = {"files": [], "verify_steps": [], "rationale": "read-only/write-locked task; no writable file plan"}
        state["file_plan"] = plan
        state["file_plan_review"] = {
            "version": "v1.20",
            "ok": True,
            "reviewed_files": [],
            "writable_files": [],
            "read_reference_files": [],
            "approval_required_files": [],
            "skipped": True,
            "reason": "read-only/write-locked task",
        }
        state["write_intents"] = build_write_intents(state, plan)
        state["needs_verification"] = False
        trace.event("file_plan_skipped_read_only", file_plan=plan, read_only=state.get("read_only"), write_locked=state.get("write_locked"))
        trace.snapshot(state)
        return state

    intent = state.get("task_intent") or classify_task_intent(state.get("task", ""), state.get("task_spec") or {})
    scope_contract = state.get("scope_contract") or intent.get("scope_contract") or {}
    repo_has_files = _workspace_has_files(state.get("repo_map") or {})
    if (
        state.get("mode") in {"write", "generate_project"}
        and repo_has_files
        and intent.get("analyze_requested")
        and not intent.get("create_requested")
        and not intent.get("create_paths")
    ):
        state["failure"] = {
            "failure_type": "write_mode_without_write_intent",
            "priority": 1,
            "message": "existing-project write route had analysis intent but no explicit current write target",
            "signature": "write_mode_without_write_intent",
            "raw_excerpt": str({"task_intent": intent, "read_only_policy": state.get("read_only_policy")})[:2000],
        }
        state["stopped_reason"] = "write_mode_without_write_intent"
        state["file_plan"] = {"files": [], "verify_steps": [], "rationale": "blocked unsafe write plan"}
        state["needs_verification"] = False
        trace.event("file_plan_blocked_no_write_intent", task_intent=intent, repo_has_files=repo_has_files)
        trace.snapshot(state)
        return state
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    user = (
        f"Task:\n{state.get('task')}\n\n"
        f"Mode: {state.get('mode')}\n"
        f"Task contract:\n{state.get('task_contract')}\n\n"
        f"Task-relevant repository evidence:\n{_planning_evidence(state)}\n\n"
        f"Prohibited artifacts:\n{(state.get('task_contract') or {}).get('prohibited_artifacts') or (state.get('task_intent') or {}).get('prohibited_artifacts') or []}\n\n"
        f"Workspace files currently present: {len((state.get('repo_map') or {}).get('files', []))}\n"
        f"Existing files sample: {((state.get('repo_map') or {}).get('files', [])[:80])}\n\n"
        "Create a file plan. If the workspace is empty and the task asks to write/generate code, plan new files directly instead of exploring."
    )
    try:
        text = client.chat([
            {"role": "system", "content": FILE_PLAN_SYSTEM},
            {"role": "user", "content": user},
        ], purpose=f"file_plan:{state.get('mode')}", max_tokens=1600)
        obj = extract_json_object(text)
        plan = FilePlan(**obj).model_dump()
        if not plan.get("files"):
            raise ValueError("file plan contains no files")
    except Exception as e:
        trace.event("file_plan_llm_failed", error=str(e), fallback=True)
        plan = _fallback_file_plan(state, str(e))
    plan["verify_steps"] = _sanitize_verify_steps(
        state,
        plan.get("verify_steps") or [],
        planned_files=plan.get("files") or [],
    )

    # sanitize paths and remove dangerous entries
    safe_files = []
    seen = set()
    for f in plan.get("files", []):
        path = str(f.get("path", "")).strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or path.startswith("/") or ".." in Path(path).parts:
            continue
        if path.startswith(".coding_agent/"):
            continue
        if path in seen:
            continue
        seen.add(path)
        safe_files.append({
            "path": path,
            "purpose": str(f.get("purpose", ""))[:500],
            "kind": f.get("kind") if f.get("kind") in {"code", "test", "readme", "config", "data", "other"} else "other",
        })
    # Preserve user-mentioned paths as output artifacts only when the
    # surrounding task text has create/write intent, or when the path is a
    # plausible code/test/readme deliverable. User-mentioned data/result files
    # such as experiments/foo_summary.json are normally input references, not
    # files to generate.
    existing_paths = {f.get("path") for f in safe_files}
    for mentioned in extract_mentioned_paths(state.get("task", "")):
        if mentioned not in existing_paths and mentioned.endswith((".py", ".md", ".json", ".jsonl", ".csv", ".txt", ".sh", ".yaml", ".yml", ".toml")):
            explicit_create = path_has_explicit_create_intent(state.get("task", ""), mentioned)
            if not explicit_create and not is_probably_code_output(mentioned):
                continue
            kind = "test" if mentioned.startswith("tests/") or Path(mentioned).name.startswith("test_") else ("readme" if Path(mentioned).name.lower().startswith("readme") or mentioned.endswith(".md") else ("code" if mentioned.endswith(".py") else "data"))
            safe_files.append({
                "path": mentioned,
                "purpose": "user-mentioned target artifact",
                "kind": kind,
                "explicit_user_requested": True,
            })
            existing_paths.add(mentioned)

    # If the user explicitly asked for output paths,
    # they must appear in the file plan. If the LLM returns an empty or incomplete
    # plan, add only those contract-required output paths. Read/reference inputs
    # are still filtered by FilePlanReview below.
    required_targets = _required_create_targets(state.get("task", ""), state.get("task_contract") or {}, state.get("task_intent") or {})
    existing = {x.get("path") for x in safe_files}
    for item in required_targets:
        if item["path"] not in existing:
            safe_files.append(item)
            existing.add(item["path"])
    if required_targets:
        required_set = {x["path"] for x in required_targets}
        # Avoid generic fallback artifacts such as main.py/test_main.py when the
        # user specified exact target files. Keep only required targets and any
        # LLM-planned file that is not a generic fallback name.
        safe_files = [x for x in safe_files if x.get("path") in required_set or x.get("path") not in {"main.py", "tests/test_main.py", "core.py", "tests/test_core.py"}]
    if required_targets and not safe_files:
        safe_files = required_targets
    safe_files = _dedupe_equivalent_test_files(safe_files)
    prohibited_artifacts = (state.get("task_contract") or {}).get("prohibited_artifacts") or (state.get("task_intent") or {}).get("prohibited_artifacts") or []
    safe_files, prohibited_entries = _filter_prohibited_files(safe_files, prohibited_artifacts)
    if prohibited_entries:
        state["prohibited_file_plan_entries"] = prohibited_entries
        trace.event("file_plan_prohibited_entries_removed", prohibited_entries=prohibited_entries, prohibited_artifacts=prohibited_artifacts)
    safe_files, protected_entries = _filter_scope_protected_files(safe_files, scope_contract)
    if protected_entries:
        state["protected_file_plan_entries"] = protected_entries
        trace.event("file_plan_scope_protected_entries_removed", protected_entries=protected_entries, scope_contract=scope_contract)

    safe_files, output_layout = apply_output_layout(state, safe_files)
    state["output_layout"] = output_layout
    plan["files"] = safe_files
    state["file_plan"] = plan
    # Generate a single write-intent registry. FilePlanReview is a
    # view over these intents; ToolExec must use the same registry instead of
    # reinterpreting glob rules independently.
    state["write_scope_policy"] = build_write_scope_policy(state.get("task", ""), state.get("mode"), bool(state.get("read_only")), plan)
    write_intents = build_write_intents(state, plan)
    state["write_intents"] = write_intents
    writable_files = []
    for i in (write_intents.get("intents") or []):
        if i.get("allowed") and i.get("operation") in {"create_new", "modify_existing"}:
            plan_item = i.get("plan_item") or {}
            mention = i.get("mention") or {}
            row = {
                "path": i.get("path"),
                "purpose": plan_item.get("purpose") or "approved write target",
                "kind": plan_item.get("kind") or i.get("role") or "other",
                "operation": i.get("operation"),
            }
            original_path = plan_item.get("original_path") or mention.get("original_path")
            if original_path:
                row["original_path"] = original_path
            writable_files.append(row)
    writable_files = _dedupe_equivalent_test_files(writable_files)
    review = {
        "version": "v1.17",
        "ok": not bool(write_intents.get("blocked_write_paths")),
        "reviewed_files": list(write_intents.get("intents") or []),
        "writable_files": writable_files,
        "read_reference_files": [i for i in (write_intents.get("intents") or []) if i.get("operation") == "read_reference"],
        "approval_required_files": [i for i in (write_intents.get("intents") or []) if i.get("operation") == "approval_required"],
    }
    state["file_plan_review"] = review
    # GenerateFiles must only write files approved by write intents. Read
    # references remain available for retrieval, but never become outputs.
    plan["files"] = [dict(x) for x in review.get("writable_files", [])]
    state["file_plan"] = plan
    refresh_verification_test_registry(state, existing_only=False)
    state["write_intents"] = build_write_intents(state, {**plan, "files": safe_files})
    state["file_plan_review"] = review
    if state.get("mode") in {"write", "generate_project", "modify", "debug", "repair_existing"} and not (review.get("writable_files") or []):
        state["failure"] = {
            "failure_type": "file_plan_no_writable_targets",
            "priority": 1,
            "message": "write-mode task produced no allowed writable files",
            "signature": "file_plan_no_writable_targets",
            "raw_excerpt": str({"task_intent": state.get("task_intent"), "file_plan_review": review})[:2000],
        }
        state["stopped_reason"] = "file_plan_no_writable_targets"
    state["needs_verification"] = False
    trace.event("file_plan_done", file_plan=plan, file_plan_review=review, write_intents=state.get("write_intents"), required_targets=required_targets, task_intent=state.get("task_intent"), output_layout=state.get("output_layout"))
    trace.snapshot(state)
    return state
