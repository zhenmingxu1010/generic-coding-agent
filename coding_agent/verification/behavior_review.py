from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coding_agent.core.llm_client import LLMTimeoutError, OpenAICompatClient
from coding_agent.core.utils import extract_json_object, truncate
from coding_agent.scope.write_scope import extract_mentioned_paths, normalize_rel
from coding_agent.scope.write_scope_audit import build_write_scope_audit
from coding_agent.verification.plan_grounding import compact_grounding_source_catalog


VERIFICATION_PLAN_SYSTEM = """You plan execution evidence for a general coding agent.
Return JSON only with this schema:
{
  "verify_steps": [
    {
      "name": "short_unique_name",
      "command": ["executable", "arg1"],
      "verifies": ["exact_requirement_id"],
      "basis": [{"source": "task|exact_requirement_id|repository/relative/path", "quote": "exact supporting excerpt"}],
      "expected": "observable result supported by the citation",
      "timeout_sec": 180,
      "success_exit_codes": [0],
      "stdin": "optional text sent to the process standard input",
      "sandbox": {
        "copy_paths": ["relative/file", "relative/glob/*"],
        "files": [{"path": "relative/fixture", "content": "temporary content"}],
        "omit_paths": ["relative/path"]
      }
    }
  ]
}
Rules:
- Add only steps needed for uncovered requirements with evidence_mode=execution.
- Return at most four minimal steps. Prefer one high-information boundary
  scenario over enumerating many already-covered examples.
- Do not add VCS or shell commands to prove runtime-evidence constraints; the
  runtime evidence supplied in the prompt is authoritative for those facts.
- Exercise public observable behavior. Compile and --help do not prove functional behavior.
- Use direct argv commands only: no shell syntax, redirection, or inline
  Python. Shell scripts may be invoked only as `sh relative/script.sh ...` or
  `bash relative/script.sh ...`, never with `-c`/`-lc`.
- To test a documented standard-input interface, provide the exact input in
  the step's `stdin` field; fixture files are not automatically piped.
- To exercise a Python callable, create a short probe script under
  `sandbox.files` and invoke it as `python relative_probe.py`; never use
  `python -c`. The probe may use assertions or deliberately leave a documented
  exception uncaught, paired with its documented `success_exit_codes`.
- Work inside the workspace. Use relative paths.
- For disposable outputs, use {verification_dir}/name.ext.
- Use a sandbox when verification needs missing/replaced inputs or another
  disposable workspace state. Copy only the files needed for the scenario.
- Use exact requirement IDs. Do not invent requirement IDs.
- Every step must bind `verifies` to at least one exact requirement ID.
- Every step must cite exact task or repository evidence in `basis` and state
  the supported observable result in `expected`.
- When a requirement delegates behavior to a repository contract file such as
  a README or schema, cite that file and exercise the behavior stated there.
- A broad existing test suite is not a direct scenario for every clause in a
  delegated contract. When the contract defines an accepted input type or
  shape, include direct positive and negative boundary scenarios. For JSON
  container requirements, exercise at least one valid value and one valid-JSON
  value of the wrong top-level type (for example object versus array).
- For a wrong container-type scenario, prefer the minimal empty value (such as
  an empty object instead of a populated object). This detects implementations
  that only reject the contained items and accidentally accept a vacuously
  iterable wrong outer type.
- A requirement-ID citation is not a substitute for the delegated document.
  If the requirement names README.md, schema.json, or another contract path,
  at least one basis item must use that exact repository path as `source`.
- Do not introduce an option, input mode, API, or behavior that is absent from
  the cited evidence.
- Public command options must be cited from the user task or repository
  evidence, not only from an LLM-generated requirement description.
- Repository source may establish how to invoke requested behavior, but its
  mere presence does not turn unrelated implementation behavior into a user
  requirement.
- `success_exit_codes` defaults to `[0]`. Include a documented nonzero code
  only when observing that code is itself required behavior.
"""


VERIFICATION_ORACLE_SYSTEM = """You review failed verification scenarios for a general coding agent.
Return JSON only:
{
  "step_reviews": [
    {"name": "exact_step_name", "status": "grounded|unsupported|ambiguous", "reason": "short reason"}
  ]
}
Rules:
- Decide whether each scenario follows from its cited task or repository evidence.
- Repository source may establish invocation syntax, but cannot by itself turn
  unrelated implementation behavior into a required feature.
- A command that exercises an unstated option, input mode, API, edge case, or
  behavior is unsupported even if it could be a useful feature.
- Judge the verification scenario, not whether the implementation could be changed to pass it.
- Use ambiguous when the supplied evidence is insufficient. Never assume an
  unstated behavior is required.
"""


VERIFICATION_REVIEW_SYSTEM = """You are the evidence reviewer of a general coding agent.
Judge each task requirement only from the supplied executed command results and
captured verification artifacts. Return JSON only:
{
  "claims": [
    {
      "atom_id": "exact_requirement_id",
      "status": "passed|failed|unverified",
      "cited_steps": ["exact_step_name"],
      "cited_runtime": ["exact_runtime_evidence_id"],
      "evidence": ["short observable excerpt"],
      "reason": "short explanation"
    }
  ]
}
Rules:
- Never infer success from source code appearance, a plan, --help, or compilation alone.
- A step may support a requirement only when its expected behavior follows
  from its cited basis. Binding `verifies` to an atom does not make an
  unrelated or expanded scenario relevant.
- For evidence_mode=execution, passed requires direct observable evidence from
  at least one successful step whose `verifies` list contains that atom ID.
- For evidence_mode=runtime, passed requires an exact cited runtime evidence
  source. Runtime facts are authoritative for write scope, generated artifact
  placement, and other agent-observed constraints.
- failed means executed evidence contradicts the requirement.
- unverified means evidence is absent or insufficient.
- Do not judge implementation style unless the requirement explicitly asks for it.
"""


def _dynamic_atoms(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        atom
        for atom in (state.get("task_contract") or {}).get("requirement_atoms") or []
        if isinstance(atom, dict)
        and atom.get("required", True)
        and str(atom.get("type") or "") not in {"artifact_exists", "write_scope"}
    ]


def _evidence_mode(atom: dict[str, Any]) -> str:
    data = atom.get("data") if isinstance(atom.get("data"), dict) else {}
    explicit = str(data.get("evidence_mode") or "").strip().lower()
    if explicit in {"execution", "runtime", "artifact", "analysis"}:
        return explicit
    return "runtime" if str(atom.get("type") or "") == "constraint" else "execution"


def build_runtime_evidence(state: dict[str, Any]) -> dict[str, Any]:
    """Expose generic facts already observed by the runtime to the reviewer."""
    return {
        "runtime:write_scope_audit": build_write_scope_audit(state),
        "runtime:generated_files": state.get("generated_files") or [],
        "runtime:changed_files": state.get("changed_files") or [],
        "runtime:output_layout": state.get("output_layout") or {},
        "runtime:test_registry": state.get("verification_test_registry") or {},
        "runtime:scope_contract": state.get("scope_contract") or {},
    }


def _covered_ids(steps: list[dict[str, Any]]) -> set[str]:
    return {
        str(atom_id)
        for step in steps
        if isinstance(step, dict)
        for atom_id in step.get("verifies") or []
        if atom_id
    }


TEST_EVIDENCE_STOPWORDS = {
    "all", "behavior", "command", "current", "existing", "file", "interface",
    "pass", "passed", "public", "python", "requirement", "run", "task", "test",
    "tests", "the", "unchanged",
}


def _test_evidence_tokens(value: Any) -> set[str]:
    text = str(value or "")
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9]*", text.lower().replace("_", " "))
        if len(token) >= 3 and token not in TEST_EVIDENCE_STOPWORDS
    }
    if "命令行" in text or re.search(r"\bcommand[\s-]*line\b", text, flags=re.I):
        tokens.add("cli")
    return tokens


def _all_tests_requirement(atom: dict[str, Any]) -> bool:
    if _contract_reference_paths(atom):
        return False
    text = " ".join([
        str(atom.get("description") or ""),
        *(str(item) for item in atom.get("evidence") or []),
    ])
    low = text.lower()
    mentions_tests = "test" in low or "测试" in text
    mentions_all = "all" in low or "全部" in text or "所有" in text
    mentions_pass = "pass" in low or "通过" in text
    return mentions_tests and mentions_all and mentions_pass


def _contract_reference_paths(atom: dict[str, Any]) -> list[str]:
    data = atom.get("data") if isinstance(atom.get("data"), dict) else {}
    text = "\n".join([
        str(atom.get("description") or ""),
        *(str(item) for item in atom.get("evidence") or []),
        str(data.get("contract_text") or ""),
    ])
    def is_contract_path(path: str) -> bool:
        candidate = Path(path)
        name = candidate.name.lower()
        if candidate.suffix.lower() in {".md", ".rst"} or name.startswith("readme"):
            return True
        return any(marker in name for marker in ("schema", "spec", "contract"))

    return [
        normalize_rel(path)
        for path in extract_mentioned_paths(text)
        if is_contract_path(path)
    ]


def _step_cites_contract_path(step: dict[str, Any], paths: list[str]) -> bool:
    cited = {
        normalize_rel(item.get("source"))
        for item in (
            list(step.get("basis") or [])
            + list((step.get("grounding") or {}).get("citations") or [])
        )
        if isinstance(item, dict) and item.get("source")
    }
    return any(path in cited or f"context:{path}" in cited for path in paths)


def _required_json_top_level_types(state: dict[str, Any], atom: dict[str, Any]) -> set[str]:
    required: set[str] = set()
    root = Path(str(state.get("workspace") or ".")).resolve()
    for rel in _contract_reference_paths(atom):
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for sentence in re.split(r"(?<=[.!?。！？])\s+", text):
            low = " ".join(sentence.lower().replace("`", "").split())
            normative = any(marker in low for marker in ("must", "required", "必须", "只能", "应为"))
            if not normative or "json" not in low:
                continue
            if "array" in low or "数组" in low:
                required.add("array")
            if "object" in low or "对象" in low:
                required.add("object")
    return required


def _has_wrong_json_top_level_scenario(
    state: dict[str, Any],
    atom: dict[str, Any],
    steps: list[dict[str, Any]],
    successful_names: set[str],
) -> bool:
    required_types = _required_json_top_level_types(state, atom)
    if not required_types:
        return True
    for step in steps:
        if str(step.get("name") or "") not in successful_names:
            continue
        if str(atom.get("id") or "") not in set(step.get("verifies") or []):
            continue
        for fixture in (step.get("sandbox") or {}).get("files") or []:
            if not isinstance(fixture, dict):
                continue
            try:
                value = json.loads(str(fixture.get("content") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if "array" in required_types and not isinstance(value, list):
                return True
            if "object" in required_types and not isinstance(value, dict):
                return True
    return False


def _pytest_requirement_evidence(state: dict[str, Any], atoms: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    successful_runs = [
        run
        for run in (state.get("test_results") or {}).get("runs") or []
        if isinstance(run, dict) and run.get("ok") and int(run.get("total", 0) or 0) > 0
    ]
    if not successful_runs:
        return {}
    passed_cases = list(dict.fromkeys(
        str(case.get("test") or "")
        for run in successful_runs
        for case in run.get("testcases") or []
        if isinstance(case, dict) and case.get("status") == "passed" and case.get("test")
    ))
    case_tokens = {case: _test_evidence_tokens(case) for case in passed_cases}
    common_tokens = set.intersection(*case_tokens.values()) if case_tokens else set()
    evidence: dict[str, dict[str, Any]] = {}
    for atom in atoms:
        if _evidence_mode(atom) != "execution":
            continue
        atom_id = str(atom.get("id") or "")
        if not atom_id:
            continue
        if _all_tests_requirement(atom):
            evidence[atom_id] = {
                "matched_testcases": passed_cases[:20],
                "reason": "the complete project pytest run passed",
            }
            continue
        # A broad contract delegated to a repository document cannot be
        # proven merely because one pytest node name shares a token such as
        # "cli". It needs a verification step grounded in that document.
        if _contract_reference_paths(atom):
            continue
        atom_tokens = _test_evidence_tokens(
            " ".join(str(atom.get(key) or "") for key in ("description", "verify_hint"))
        )
        matched = [
            case
            for case, tokens in case_tokens.items()
            if atom_tokens.intersection(tokens - common_tokens)
        ]
        if matched:
            evidence[atom_id] = {
                "matched_testcases": matched[:20],
                "reason": "passing project test names directly match the requirement",
            }
    return evidence


def _string_list(value: Any, *, limit: int = 500) -> list[str]:
    """Normalize an LLM scalar-or-list field without splitting strings."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item)[:limit] for item in values if item is not None and str(item)]


def _failed_execution_claims(
    atoms: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    """Produce grounded claims without an LLM when execution already failed."""
    result_by_name = {
        str(result.get("name")): result
        for result in results
        if isinstance(result, dict)
    }
    failed_results = {
        name: result
        for name, result in result_by_name.items()
        if result.get("executed", True)
        and (int(result.get("returncode", 1) or 0) != 0 or result.get("timed_out"))
    }
    unexecuted_results = {
        name: result
        for name, result in result_by_name.items()
        if not result.get("executed", True)
    }
    if not failed_results and not unexecuted_results:
        return None

    bound_by_atom: dict[str, list[str]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or "")
        if name not in result_by_name:
            continue
        for atom_id in step.get("verifies") or []:
            bound_by_atom.setdefault(str(atom_id), []).append(name)

    claims: dict[str, dict[str, Any]] = {}
    for atom in atoms:
        atom_id = str(atom.get("id") or "")
        bound_steps = bound_by_atom.get(atom_id, [])
        bound_failures = [name for name in bound_steps if name in failed_results]
        bound_unexecuted = [name for name in bound_steps if name in unexecuted_results]
        bound_successes = [
            name
            for name in bound_steps
            if name in result_by_name
            and result_by_name[name].get("executed", True)
            and int(result_by_name[name].get("returncode", 1) or 0) == 0
            and not result_by_name[name].get("timed_out")
        ]
        if bound_failures:
            evidence = []
            for name in bound_failures[:3]:
                result = failed_results[name]
                output = str(result.get("stderr") or result.get("stdout") or "").strip()
                evidence.append(
                    f"{name} failed with returncode={result.get('returncode')}: "
                    f"{truncate(output, 700)}"
                )
            claims[atom_id] = {
                "atom_id": atom_id,
                "status": "failed",
                "cited_steps": bound_failures,
                "cited_runtime": [],
                "evidence": evidence,
                "reason": "an explicitly requirement-bound verification step failed",
            }
        elif bound_successes:
            evidence = []
            for name in bound_successes[:3]:
                result = result_by_name[name]
                output = str(result.get("stdout") or result.get("stderr") or "").strip()
                evidence.append(
                    f"{name} passed with returncode=0: {truncate(output, 700)}"
                )
            claims[atom_id] = {
                "atom_id": atom_id,
                "status": "passed",
                "cited_steps": bound_successes,
                "cited_runtime": [],
                "evidence": evidence,
                "reason": "all explicitly requirement-bound verification steps completed successfully",
            }
        elif bound_unexecuted:
            evidence = [
                f"{name} was not executed: "
                f"{truncate(str(unexecuted_results[name].get('stderr') or ''), 700)}"
                for name in bound_unexecuted[:3]
            ]
            claims[atom_id] = {
                "atom_id": atom_id,
                "status": "unverified",
                "cited_steps": bound_unexecuted,
                "cited_runtime": [],
                "evidence": evidence,
                "reason": "the planned verification command could not be executed",
            }
        else:
            claims[atom_id] = {
                "atom_id": atom_id,
                "status": "unverified",
                "cited_steps": [],
                "cited_runtime": [],
                "evidence": [],
                "reason": "verification stopped with failed execution before this requirement could be confirmed",
            }
    return claims


def _compact_results_for_review(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": result.get("name"),
            "command": result.get("command"),
            "normalized_returncode": result.get("returncode"),
            "actual_returncode": (
                result.get("actual_returncode")
                if result.get("actual_returncode") is not None
                else result.get("returncode")
            ),
            "success_exit_codes": result.get("success_exit_codes") or [0],
            "timed_out": bool(result.get("timed_out")),
            "stdout": truncate(str(result.get("stdout") or ""), 1800),
            "stderr": truncate(str(result.get("stderr") or ""), 1200),
            "executed": bool(result.get("executed", True)),
            "failure_kind": str(result.get("failure_kind") or ""),
        }
        for result in results
        if isinstance(result, dict)
    ]


def _compact_artifacts_for_review(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": artifact.get("path"),
            "size": artifact.get("size"),
            "preview": truncate(str(artifact.get("preview") or ""), 1200),
            "truncated": artifact.get("truncated"),
        }
        for artifact in artifacts[:12]
        if isinstance(artifact, dict)
    ]


def review_failed_verification_oracles(
    state: dict[str, Any],
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Check failed task-specific scenarios before blaming implementation."""
    step_by_name = {
        str(step.get("name") or ""): step
        for step in steps
        if isinstance(step, dict) and step.get("name")
    }
    failed = [
        result
        for result in results
        if isinstance(result, dict)
        and str(result.get("name") or "") in step_by_name
        and result.get("executed", True)
        and (int(result.get("returncode", 1) or 0) != 0 or result.get("timed_out"))
    ]
    if not failed:
        return {}

    reviews: dict[str, dict[str, str]] = {}
    candidates: list[dict[str, Any]] = []
    for result in failed:
        name = str(result.get("name") or "")
        step = step_by_name[name]
        grounding = step.get("grounding") if isinstance(step.get("grounding"), dict) else {}
        if grounding.get("status") != "accepted":
            reviews[name] = {
                "name": name,
                "status": "unsupported",
                "reason": "the scenario has no accepted task or project grounding",
            }
            continue
        candidates.append({
            "step": step,
            "result": {
                "returncode": result.get("returncode"),
                "stdout": truncate(str(result.get("stdout") or ""), 1200),
                "stderr": truncate(str(result.get("stderr") or ""), 1200),
                "timed_out": bool(result.get("timed_out")),
            },
        })

    if not candidates:
        return reviews

    prompt = (
        f"Task:\n{state.get('task')}\n\n"
        f"Requirements:\n{truncate(json.dumps(_dynamic_atoms(state), ensure_ascii=False), 6500)}\n\n"
        f"Failed scenarios:\n{truncate(json.dumps(candidates, ensure_ascii=False), 9000)}\n\n"
        f"Grounding sources:\n"
        f"{truncate(json.dumps(compact_grounding_source_catalog(state), ensure_ascii=False), 9000)}"
    )
    state["verification_oracle_prompt_chars"] = len(prompt)
    try:
        client = OpenAICompatClient("configs/model.yaml", messages_path=state.get("messages_path"))
        text = client.chat(
            [
                {"role": "system", "content": VERIFICATION_ORACLE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            purpose="verification_oracle_review",
            max_tokens=1000,
        )
        raw_rows = extract_json_object(text).get("step_reviews") or []
    except Exception as exc:
        state.setdefault("verification_review_errors", []).append(f"oracle review: {str(exc)[:1800]}")
        raw_rows = []

    candidate_names = {str(row["step"].get("name") or "") for row in candidates}
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        status = str(raw.get("status") or "ambiguous").lower()
        if name not in candidate_names or status not in {"grounded", "unsupported", "ambiguous"}:
            continue
        reason = str(raw.get("reason") or "")[:1200]
        # The oracle reviewer judges whether a scenario follows from the task,
        # not whether the current implementation already supports it.  Models
        # occasionally invert that distinction and reject a correctly grounded
        # scenario merely because its execution exposed a bug.  Preserve such
        # failures as implementation evidence; unsupported scenarios still
        # remain rejectable when the reason points to absent/unstated contract
        # behavior.
        implementation_words = (
            "current implementation",
            "the implementation",
            "implementation's",
            "implementation does not",
            "implementation fails",
            "function fails",
            "code fails",
            "runtime error",
            "traceback",
        )
        if status != "grounded" and any(word in reason.lower() for word in implementation_words):
            status = "grounded"
            reason = "accepted grounding must not be rejected because the current implementation failed"
        reviews[name] = {
            "name": name,
            "status": status,
            "reason": reason,
        }
    for name in candidate_names:
        reviews.setdefault(name, {
            "name": name,
            "status": "ambiguous",
            "reason": "the failed scenario could not be confirmed from supplied requirements and evidence",
        })
    return reviews


def supplement_verification_steps(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.setdefault("file_plan", {})
    steps = [dict(step) for step in plan.get("verify_steps") or [] if isinstance(step, dict)]
    atoms = [atom for atom in _dynamic_atoms(state) if _evidence_mode(atom) == "execution"]
    prior_results = [
        result
        for result in (state.get("verification") or {}).get("results") or []
        if isinstance(result, dict)
    ]
    rejected_prior_steps = set(
        (state.get("verification_oracle_review") or {}).get("rejected_step_names") or []
    )
    failed_prior_steps = [
        str(result.get("name") or "")
        for result in prior_results
        if result.get("executed", True)
        and (int(result.get("returncode", 1) or 0) != 0 or result.get("timed_out"))
        and str(result.get("name") or "") not in rejected_prior_steps
    ]
    if failed_prior_steps:
        return {
            "added": [],
            "missing": [],
            "requested_requirement_ids": [],
            "skipped": True,
            "reason": "repair existing failed execution before expanding the verification plan",
            "failed_prior_steps": failed_prior_steps,
        }
    prior_claims = state.get("verification_claims") or {}
    if prior_claims:
        missing = [
            atom
            for atom in atoms
            if str((prior_claims.get(str(atom.get("id"))) or {}).get("status") or "unverified") == "unverified"
        ]
    else:
        missing = [atom for atom in atoms if str(atom.get("id")) not in _covered_ids(steps)]
    if not missing:
        return {"added": [], "missing": [], "requested_requirement_ids": []}
    if int(state.get("verification_plan_attempts", 0) or 0) >= 2:
        return {
            "added": [],
            "missing": [str(atom.get("id")) for atom in missing],
            "requested_requirement_ids": [str(atom.get("id")) for atom in missing],
            "exhausted": True,
        }

    state["verification_plan_attempts"] = int(state.get("verification_plan_attempts", 0) or 0) + 1
    client = OpenAICompatClient("configs/model.yaml", messages_path=state.get("messages_path"))
    replan_guidance = ""
    if prior_claims:
        contract_paths = sorted({
            path
            for atom in missing
            for path in _contract_reference_paths(atom)
        })
        replan_guidance = (
            "Previous evidence was reviewed as unverified. Replace broad suite-level evidence "
            "with direct public scenarios for each missing behavior; do not resubmit the same "
            "pytest command. Return no more than four steps and do not use python -c; put Python "
            "callable probes in sandbox.files and run the script by relative path. Focus only "
            "on documented behavior not named by the prior executed test cases. For any documented "
            "input type or shape, exercise the minimal empty wrong-type boundary value using sandbox "
            "fixture files so item validation cannot accidentally make the scenario pass. "
            f"Authoritative delegated contract paths: {contract_paths or ['<none>']}.\n\n"
        )
    prior_testcases = [
        str(case.get("test") or "")
        for run in ((state.get("verification") or {}).get("test_results") or {}).get("runs") or []
        if isinstance(run, dict)
        for case in run.get("testcases") or []
        if isinstance(case, dict) and case.get("test")
    ][:80]
    prompt = (
        f"Task:\n{state.get('task')}\n\n"
        f"{replan_guidance}"
        f"Missing requirements:\n{json.dumps(missing, ensure_ascii=False, indent=2)}\n\n"
        f"Existing verification steps:\n{json.dumps(steps, ensure_ascii=False, indent=2)}\n\n"
        f"Prior executed test cases (names only):\n{json.dumps(prior_testcases, ensure_ascii=False)}\n\n"
        f"Runtime evidence sources (do not duplicate with commands):\n"
        f"{json.dumps(build_runtime_evidence(state), ensure_ascii=False, indent=2)}\n\n"
        f"Task-relevant context:\n{truncate(state.get('context_summary', ''), 8000)}"
        f"\n\nAvailable grounding sources:\n"
        f"{truncate(json.dumps(compact_grounding_source_catalog(state), ensure_ascii=False), 9000)}"
    )
    try:
        text = client.chat(
            [
                {"role": "system", "content": VERIFICATION_PLAN_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            purpose="verification_plan",
            max_tokens=1800,
        )
        obj = extract_json_object(text)
        proposed = obj.get("verify_steps") or []
    except (Exception, LLMTimeoutError) as exc:
        state.setdefault("verification_plan_errors", []).append(str(exc)[:2000])
        return {
            "added": [],
            "missing": [str(atom.get("id")) for atom in missing],
            "requested_requirement_ids": [str(atom.get("id")) for atom in missing],
            "error": str(exc),
        }

    # Import here to avoid coupling the file planner to the verifier at module load time.
    from coding_agent.nodes.file_plan import _sanitize_verify_steps

    clean = _sanitize_verify_steps(state, proposed)
    existing_names = {str(step.get("name")) for step in steps}
    added = [step for step in clean if str(step.get("name")) not in existing_names]
    plan["verify_steps"] = steps + added
    state["file_plan"] = plan
    remaining = [
        str(atom.get("id"))
        for atom in atoms
        if str(atom.get("id")) not in _covered_ids(plan["verify_steps"])
    ]
    return {
        "added": added,
        "missing": remaining,
        "requested_requirement_ids": [str(atom.get("id")) for atom in missing],
    }


def collect_verification_artifacts(state: dict[str, Any], max_files: int = 20) -> list[dict[str, Any]]:
    root = Path(state.get("verification_artifacts_dir") or "")
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    def is_output(path: Path) -> bool:
        parts = path.relative_to(root).parts
        if not parts or parts[0] != "sandboxes":
            return True
        return len(parts) >= 4 and parts[2] == ".verification"

    files = [p for p in root.rglob("*") if p.is_file() and is_output(p)]
    for path in sorted(files)[:max_files]:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        preview = raw[:12000].decode("utf-8", errors="replace")
        out.append({
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "size": len(raw),
            "preview": preview,
            "truncated": len(raw) > 12000,
        })
    return out


def review_behavior_evidence(
    state: dict[str, Any],
    results: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    atoms = _dynamic_atoms(state)
    if not atoms:
        return {}
    steps = [step for step in (state.get("file_plan") or {}).get("verify_steps") or [] if isinstance(step, dict)]
    pytest_evidence = _pytest_requirement_evidence(state, atoms)
    if pytest_evidence:
        steps.append({
            "name": "pytest",
            "verifies": sorted(pytest_evidence),
            "expected": "The matching existing project tests pass.",
            "grounding": {"status": "accepted", "source": "project_test_suite"},
        })
    step_by_name = {str(step.get("name")): step for step in steps}
    result_by_name = {str(result.get("name")): result for result in results}
    oracle_reviews = review_failed_verification_oracles(state, steps, results)
    state["verification_oracle_review"] = {
        "steps": oracle_reviews,
        "rejected_step_names": sorted(
            name for name, review in oracle_reviews.items()
            if review.get("status") != "grounded"
        ),
    }
    rejected_names = set(state["verification_oracle_review"]["rejected_step_names"])
    if rejected_names:
        plan = dict(state.get("file_plan") or {})
        plan["verify_steps"] = [
            step
            for step in steps
            if str(step.get("name") or "") not in rejected_names
        ]
        state["file_plan"] = plan
        state["verification_oracle_review"]["removed_step_names"] = sorted(rejected_names)
    trusted_steps = [
        step
        for step in steps
        if str(step.get("name") or "") not in rejected_names
    ]
    failed_claims = _failed_execution_claims(atoms, trusted_steps, results)
    if failed_claims is not None:
        state["verification_review_mode"] = "deterministic_failed_execution"
        state["verification_review_prompt_chars"] = 0
        return failed_claims

    runtime_evidence = build_runtime_evidence(state)
    compact_results = _compact_results_for_review(results)
    compact_artifacts = _compact_artifacts_for_review(artifacts)
    prompt = (
        f"Task:\n{state.get('task')}\n\n"
        f"Requirements:\n{truncate(json.dumps(atoms, ensure_ascii=False), 7000)}\n\n"
        f"Verification steps:\n{truncate(json.dumps(steps, ensure_ascii=False), 7000)}\n\n"
        f"Executed results:\n{truncate(json.dumps(compact_results, ensure_ascii=False), 9000)}\n\n"
        f"Captured internal artifacts:\n{truncate(json.dumps(compact_artifacts, ensure_ascii=False), 6000)}\n\n"
        f"Runtime evidence:\n{truncate(json.dumps(runtime_evidence, ensure_ascii=False), 7000)}"
    )
    state["verification_review_mode"] = "llm_success_evidence_review"
    state["verification_review_prompt_chars"] = len(prompt)
    try:
        client = OpenAICompatClient("configs/model.yaml", messages_path=state.get("messages_path"))
        text = client.chat(
            [
                {"role": "system", "content": VERIFICATION_REVIEW_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            purpose="verification_review",
            max_tokens=2200,
        )
        obj = extract_json_object(text)
        raw_claims = obj.get("claims") or []
    except Exception as exc:
        state.setdefault("verification_review_errors", []).append(str(exc)[:2000])
        raw_claims = []

    allowed_ids = {str(atom.get("id")) for atom in atoms}
    atom_by_id = {str(atom.get("id")): atom for atom in atoms}
    claims: dict[str, dict[str, Any]] = {}
    for raw in raw_claims:
        if not isinstance(raw, dict):
            continue
        atom_id = str(raw.get("atom_id") or "")
        if atom_id not in allowed_ids:
            continue
        cited = _string_list(raw.get("cited_steps"), limit=200)
        cited_runtime = _string_list(raw.get("cited_runtime"), limit=200)
        valid_runtime = [name for name in cited_runtime if name in runtime_evidence]
        valid_steps = []
        successful_steps = []
        for name in cited:
            step = step_by_name.get(name)
            result = result_by_name.get(name)
            if not step or atom_id not in set(step.get("verifies") or []) or not result:
                continue
            valid_steps.append(name)
            if int(result.get("returncode", 1) or 0) == 0 and not result.get("timed_out"):
                successful_steps.append(name)
        status = str(raw.get("status") or "unverified")
        if status not in {"passed", "failed", "unverified"}:
            status = "unverified"
        evidence_mode = _evidence_mode(atom_by_id[atom_id])
        if status == "passed":
            if evidence_mode == "execution":
                contract_paths = _contract_reference_paths(atom_by_id[atom_id])
                if contract_paths:
                    successful_steps = [
                        name
                        for name in successful_steps
                        if _step_cites_contract_path(step_by_name[name], contract_paths)
                    ]
                    if successful_steps and not _has_wrong_json_top_level_scenario(
                        state,
                        atom_by_id[atom_id],
                        trusted_steps,
                        set(successful_steps),
                    ):
                        successful_steps = []
                if not successful_steps:
                    status = "unverified"
            elif evidence_mode == "runtime" and not valid_runtime:
                status = "unverified"
        if status == "failed" and not valid_steps and not valid_runtime:
            status = "unverified"
        claims[atom_id] = {
            "atom_id": atom_id,
            "status": status,
            "cited_steps": valid_steps,
            "cited_runtime": valid_runtime,
            "evidence": _string_list(raw.get("evidence"), limit=500),
            "reason": str(raw.get("reason") or "")[:1000],
        }

    for atom_id in allowed_ids:
        claims.setdefault(atom_id, {
            "atom_id": atom_id,
            "status": "unverified",
            "cited_steps": [],
            "cited_runtime": [],
            "evidence": [],
            "reason": "no grounded verification claim was produced",
        })
    for atom_id, item in pytest_evidence.items():
        claims[atom_id] = {
            "atom_id": atom_id,
            "status": "passed",
            "cited_steps": ["pytest"],
            "cited_runtime": [],
            "evidence": [
                "passing tests: " + ", ".join(item.get("matched_testcases") or [])
            ],
            "reason": str(item.get("reason") or "matching project tests passed"),
        }

    # Agent-default execution requirements are aggregate quality gates, not
    # additional user behavior. Once a directly grounded user requirement has
    # passed through a successful execution step, requiring the planner to
    # invent a duplicate command adds cost and can expand the requested scope.
    passed_user_execution_ids = {
        str(atom.get("id") or "")
        for atom in atoms
        if str(atom.get("source") or "") != "agent_implementation_default"
        and _evidence_mode(atom) == "execution"
        and (claims.get(str(atom.get("id") or "")) or {}).get("status") == "passed"
    }
    aggregate_steps: list[str] = []
    for step in trusted_steps:
        name = str(step.get("name") or "")
        result = result_by_name.get(name) or {}
        if not passed_user_execution_ids.intersection(
            str(atom_id) for atom_id in step.get("verifies") or []
        ):
            continue
        if (
            (step.get("grounding") or {}).get("status") == "accepted"
            and result.get("executed", True)
            and int(result.get("returncode", 1) or 0) == 0
            and not result.get("timed_out")
        ):
            aggregate_steps.append(name)

    if aggregate_steps:
        for atom in atoms:
            atom_id = str(atom.get("id") or "")
            data = atom.get("data") if isinstance(atom.get("data"), dict) else {}
            current = claims.get(atom_id) or {}
            if (
                str(atom.get("source") or "") == "agent_implementation_default"
                and str(data.get("contract_source") or "") == "agent_defaults"
                and atom_id in {
                    "implementation:requested_change_execution",
                    "implementation:representative_execution",
                }
                and current.get("status") == "unverified"
            ):
                claims[atom_id] = {
                    "atom_id": atom_id,
                    "status": "passed",
                    "cited_steps": aggregate_steps[:4],
                    "cited_runtime": [],
                    "evidence": [
                        "grounded execution already passed for explicit task requirement(s): "
                        + ", ".join(sorted(passed_user_execution_ids))
                    ],
                    "reason": "the agent-default execution gate is satisfied by grounded task behavior evidence",
                }

    # A generic CLI usability default may be proven by an accepted usage/help
    # invocation even when the planner bound that step only to the user's
    # positional-argument requirement.
    usage_steps: list[str] = []
    for step in trusted_steps:
        name = str(step.get("name") or "")
        result = result_by_name.get(name) or {}
        command_text = " ".join(str(part) for part in step.get("command") or [])
        observed = "\n".join([
            str(step.get("expected") or ""),
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
        ]).lower()
        exposes_usage = (
            "--help" in command_text
            or " -h" in f" {command_text}"
            or "usage" in observed
            or "用法" in observed
        )
        if (
            exposes_usage
            and (step.get("grounding") or {}).get("status") == "accepted"
            and result.get("executed", True)
            and int(result.get("returncode", 1) or 0) == 0
            and not result.get("timed_out")
        ):
            usage_steps.append(name)
    usable_cli = claims.get("implementation:usable_cli_invocation") or {}
    if usage_steps and usable_cli.get("status") == "unverified":
        claims["implementation:usable_cli_invocation"] = {
            "atom_id": "implementation:usable_cli_invocation",
            "status": "passed",
            "cited_steps": usage_steps[:2],
            "cited_runtime": [],
            "evidence": ["accepted CLI invocation exposed observable usage guidance"],
            "reason": "the agent-default CLI usability gate is satisfied by an observed usage path",
        }
    return claims
