from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from coding_agent.scope.write_scope import extract_mentioned_paths, normalize_rel
from coding_agent.workspace.run_paths import is_test_like_path


MIN_QUOTE_CHARS = 8
MAX_SOURCE_CHARS = 100_000
PYTHON_EXECUTABLES = {"python", "python.exe", "python3", "python3.exe"}


def _normalize_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?m)^\s*\d+\s*:\s?", "", text)
    # Models commonly omit presentation-only Markdown markers while preserving
    # the quoted words. Those markers do not change citation meaning.
    text = text.replace("`", "").replace("**", "").replace("__", "")
    return " ".join(text.split())


def _quote_present(quote: str, source_text: str) -> bool:
    normalized_quote = _normalize_text(quote)
    normalized_source = _normalize_text(source_text)
    if normalized_quote in normalized_source:
        return True
    # A planner may combine several exact nearby contract sentences while
    # omitting an irrelevant sentence between them. Accept only when every
    # substantial sentence is present in the cited source in the same order.
    fragments = [
        fragment.strip()
        for fragment in re.split(r"(?<=[.!?。！？])\s+", normalized_quote)
        if len(fragment.strip()) >= MIN_QUOTE_CHARS
    ]
    if len(fragments) < 2:
        return False
    cursor = 0
    for fragment in fragments:
        index = normalized_source.find(fragment, cursor)
        if index < 0:
            return False
        cursor = index + len(fragment)
    return True


def _atom_text(atom: dict[str, Any]) -> str:
    data = atom.get("data") if isinstance(atom.get("data"), dict) else {}
    evidence = list(atom.get("evidence") or [])
    if str(atom.get("source") or "") == "llm_task_requirement" and evidence:
        # LLM descriptions and verification hints are useful planning context,
        # but only the preserved user excerpts are authoritative citations.
        parts = [*evidence, data.get("contract_text")]
    else:
        parts = [atom.get("description"), *evidence, data.get("contract_text")]
    return "\n".join(str(part) for part in parts if part)


def _atom_contract_paths(atom: dict[str, Any]) -> list[str]:
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

    return list(dict.fromkeys(
        normalize_rel(path)
        for path in extract_mentioned_paths(text)
        if is_contract_path(path)
    ))


def build_grounding_sources(state: dict[str, Any]) -> dict[str, str]:
    """Return task-specific sources that a verification step may cite.

    Source IDs are structural rather than domain-specific: the user task, exact
    requirement IDs, context-pack paths, and workspace-relative files.
    """
    sources: dict[str, str] = {"task": str(state.get("task") or "")}
    for atom in (state.get("task_contract") or {}).get("requirement_atoms") or []:
        if not isinstance(atom, dict) or not atom.get("id"):
            continue
        sources[str(atom["id"])] = _atom_text(atom)

    for block in (state.get("context_pack") or {}).get("evidence_blocks") or []:
        if not isinstance(block, dict):
            continue
        path = str(block.get("path") or "").replace("\\", "/")
        content = str(block.get("content") or "")
        if path and content:
            sources[path] = content[:MAX_SOURCE_CHARS]
            sources[f"context:{path}"] = content[:MAX_SOURCE_CHARS]

    summary = str(state.get("context_summary") or "")
    if summary:
        sources["context_summary"] = summary[:MAX_SOURCE_CHARS]

    candidate_paths: list[str] = []
    for value in state.get("changed_files") or []:
        if value:
            candidate_paths.append(str(value))
    for item in state.get("generated_files") or []:
        if isinstance(item, dict) and item.get("path"):
            candidate_paths.append(str(item["path"]))
    for value in candidate_paths:
        source_id = value.replace("\\", "/")
        if source_id in sources:
            continue
        content = _workspace_source(state, source_id)
        if content is not None:
            sources[source_id] = content[:MAX_SOURCE_CHARS]
    return sources


def _workspace_source(state: dict[str, Any], source_id: str) -> str | None:
    root_value = state.get("workspace")
    if not root_value:
        return None
    rel = str(source_id or "").replace("\\", "/")
    if rel.startswith("context:"):
        rel = rel[len("context:"):]
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
        return None
    try:
        root = Path(str(root_value)).resolve()
        path = (root / rel).resolve()
        path.relative_to(root)
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_CHARS:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def resolve_grounding_source(
    state: dict[str, Any],
    source_id: str,
    sources: dict[str, str] | None = None,
) -> str | None:
    catalog = sources if sources is not None else build_grounding_sources(state)
    if source_id in catalog:
        return catalog[source_id]
    return _workspace_source(state, source_id)


def _application_option_tokens(command: list[str]) -> list[str]:
    if not command:
        return []
    executable = Path(str(command[0])).name.lower()
    start = 1
    if executable in PYTHON_EXECUTABLES:
        if len(command) > 2 and command[1] == "-m":
            start = 3
        else:
            start = 2
    options: list[str] = []
    for token in command[start:]:
        value = str(token)
        if not re.match(r"^--[A-Za-z0-9]|^-[A-Za-z]", value):
            continue
        option = value.split("=", 1)[0]
        if option not in options:
            options.append(option)
    return options


def _application_positional_inputs(command: list[str]) -> list[str]:
    if not command:
        return []
    executable = Path(str(command[0])).name.lower()
    if executable in {"pytest", "pytest.exe"}:
        return []
    start = 1
    if executable in PYTHON_EXECUTABLES:
        if len(command) > 2 and command[1] == "-m":
            if str(command[2]).lower() == "pytest":
                return []
            start = 3
        else:
            start = 2
    return [
        str(token).replace("\\", "/")
        for token in command[start:]
        if str(token)
        and not str(token).startswith("-")
        and "{verification_dir}" not in str(token)
    ]


def validate_step_grounding(state: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    sources = build_grounding_sources(state)
    raw_basis = step.get("basis") if isinstance(step.get("basis"), list) else []
    citations: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    cited_primary_text: list[str] = []
    atom_by_id = {
        str(atom.get("id") or ""): atom
        for atom in (state.get("task_contract") or {}).get("requirement_atoms") or []
        if isinstance(atom, dict) and atom.get("id")
    }
    requirement_ids = set(atom_by_id)

    for raw in raw_basis:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("source") or "").strip()
        quote = str(raw.get("quote") or "").strip()
        source_text = resolve_grounding_source(state, source_id, sources)
        if not source_id or len(_normalize_text(quote)) < MIN_QUOTE_CHARS:
            invalid.append({"source": source_id, "quote": quote, "reason": "citation is incomplete"})
            continue
        if source_text is None:
            invalid.append({"source": source_id, "quote": quote, "reason": "source does not exist"})
            continue
        if not _quote_present(quote, source_text):
            invalid.append({"source": source_id, "quote": quote, "reason": "quote is not present in source"})
            continue
        citations.append({"source": source_id, "quote": quote})
        if source_id == "task" or (
            source_id not in requirement_ids
            and source_id != "context_summary"
        ):
            cited_primary_text.append(quote)

    expected = str(step.get("expected") or "").strip()
    reasons: list[str] = []
    if not citations:
        reasons.append("verification step has no valid source citation")
    if not expected:
        reasons.append("verification step has no expected observable result")

    citation_sources = {
        normalize_rel(
            str(citation.get("source") or "").removeprefix("context:")
        )
        for citation in citations
    }
    missing_contract_citations: dict[str, list[str]] = {}
    for atom_id in step.get("verifies") or []:
        atom = atom_by_id.get(str(atom_id))
        if not atom:
            continue
        contract_paths = _atom_contract_paths(atom)
        if contract_paths and not any(path in citation_sources for path in contract_paths):
            missing_contract_citations[str(atom_id)] = contract_paths
    if missing_contract_citations:
        reasons.append("document-delegated requirements must cite their repository contract files")

    changed_or_generated = {
        normalize_rel(str(path))
        for path in state.get("changed_files") or []
        if path
    }
    changed_or_generated.update(
        normalize_rel(str(item.get("path") or ""))
        for item in state.get("generated_files") or []
        if isinstance(item, dict) and item.get("path")
    )
    implementation_only_citations = [
        citation
        for citation in citations
        if normalize_rel(str(citation.get("source") or "").removeprefix("context:"))
        in changed_or_generated
    ]
    if citations and len(implementation_only_citations) == len(citations):
        reasons.append("generated or modified implementation files cannot create new required behavior")

    primary_evidence_text = "\n".join(cited_primary_text)
    unsupported_options = [
        option
        for option in _application_option_tokens([str(part) for part in step.get("command") or []])
        if option not in primary_evidence_text
    ]
    if unsupported_options:
        reasons.append("public command options are absent from cited task or project evidence")

    cited_text = _normalize_text("\n".join(
        f"{citation['source']} {citation['quote']}"
        for citation in citations
    ))
    declared_fixture_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in ((step.get("sandbox") or {}).get("files") or [])
        if isinstance(item, dict) and item.get("path")
    }
    unsupported_test_inputs = [
        value
        for value in _application_positional_inputs([str(part) for part in step.get("command") or []])
        if is_test_like_path(value)
        and value not in declared_fixture_paths
        and _normalize_text(value) not in cited_text
    ]
    if unsupported_test_inputs:
        reasons.append("test source files may not be used as application inputs without exact evidence")

    return {
        "status": "accepted" if not reasons else "rejected",
        "citations": citations,
        "invalid_citations": invalid,
        "expected": expected,
        "unsupported_options": unsupported_options,
        "unsupported_test_inputs": unsupported_test_inputs,
        "missing_contract_citations": missing_contract_citations,
        "implementation_only_citations": implementation_only_citations,
        "reasons": reasons,
    }


def compact_grounding_source_catalog(state: dict[str, Any], max_chars: int = 9000) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    remaining = max_chars
    for source_id, content in build_grounding_sources(state).items():
        if remaining <= 200:
            break
        preview = str(content)[: min(1800, remaining)]
        rows.append({"source": source_id, "content": preview})
        remaining -= len(preview)
    return rows
