from __future__ import annotations

import json
import os
import re
import fnmatch
from pathlib import Path
from typing import Any

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.workspace.repo_map import ROLES
from coding_agent.core.schemas import VerificationResult
from coding_agent.core.utils import extract_json_object, truncate, write_json
from coding_agent.memory.project_memory import update_project_memory_from_analysis, compact_project_memory_for_prompt
from coding_agent.ux.language import language_instruction_for_text, response_language_quality
from coding_agent.contracts.analysis_contract import (
    _section_present,
    extract_report_headings,
    analysis_path_mentions,
    build_analysis_contract,
    collect_analysis_evidence_paths,
    verify_analysis_report_against_contract,
)
from .common import get_trace


BASE_REPORT_SYSTEM = """You are a senior codebase-analysis agent.
Generate a concrete analysis report based only on provided repository map, selected evidence, role summaries, snippets, and long-term project memory.
Do not claim unsupported facts. If uncertain, say uncertain.
Every major claim should mention concrete evidence paths.
Use long-term project memory as hints only; file snippets and repository map are stronger evidence.
Do not use generic metric names such as precision/recall/F1 unless those exact metrics appear in evidence.
Output Markdown only.
"""

OVERVIEW_REPORT_INSTRUCTIONS = """Use this report shape:
# Repository Analysis Report
## 1. Overall Purpose
## 2. Project Type and Evidence Coverage
## 3. Directory Structure
## 4. Main Entry Points
## 5. Data Flow and Inputs/Outputs
## 6. Model / Core Logic
## 7. Losses / Metrics / Evaluation
## 8. Scripts / Workflow / Execution
## 9. Configs, Results, and Artifacts
## 10. Risks, Gaps, and Next Checks
## 11. File Responsibility Table
## 12. Question Coverage Checklist
"""

METRIC_RESULT_REPORT_INSTRUCTIONS = """Use this task-focused report shape, not a generic repository overview:
# Metrics / Results / Summary Analysis Report
## 1. Relevant Files
List metrics, result/summary, collection/aggregation, config, and script files with concrete paths.
## 2. Metric Schema
List concrete metric names found in evidence. For each metric, state likely direction when evidence or common metric semantics support it: higher-is-better, lower-is-better, or unknown. Do not invent metrics.
## 3. Result/Summary Schema
Describe result JSON/summary fields and file patterns found in evidence.
## 4. Existing Aggregation Flow
Explain how current collection/summary code or scripts work, if evidence exists.
## 5. Model/Run Comparison Method
Give a concrete comparison method: input files, grouping axes, ranking/sorting logic, and output table columns.
## 6. Proposed Summary Analysis Script Design
Design the next script with script name, input files, parsed fields, output columns/tables, sorting/ranking logic, and usage command. Mark uncertain fields explicitly.
## 7. Risks and Missing Evidence
List missing files/unknown schema/risks.
## 8. Question Coverage Checklist
For every required question from the analysis contract, state Answered/Partial/Missing and cite evidence paths.
"""

REPORT_COMPLETENESS_CONSTRAINTS = """Hard output constraints:
- The report must include every required top-level section before adding detail.
- Do not spend the output budget on long code blocks or long JSON excerpts.
- Prefer compact bullets and short tables; cap each major section at roughly 4-8 bullets unless the task explicitly asks for exhaustive detail.
- If output space is limited, keep later required sections instead of expanding earlier sections.
- Always include the Risks / Gaps section, File Responsibility Table, and Question Coverage Checklist when requested by the report shape.
"""

REQUIRED_OVERVIEW_HEADINGS = [
    "Overall Purpose",
    "Project Type",
    "Directory Structure",
    "Main Entry Points",
    "Data Flow",
    "Model",
    "Losses",
    "Scripts",
    "Configs",
    "Risks",
    "File Responsibility",
]

CORE_EVIDENCE_SYMBOL_ROLES = {
    "data_pipeline",
    "model_definition",
    "loss_definition",
    "metric_evaluation",
}
PATH_REQUIRED_EVIDENCE_ROLES = CORE_EVIDENCE_SYMBOL_ROLES | {
    "entrypoint",
    "run_workflow",
    "results_or_outputs",
    "config_or_arguments",
}
STRUCTURED_RESULT_ROLES = {"results_or_outputs", "config_or_arguments"}
INTERCHANGEABLE_PATH_EVIDENCE_ROLES = {"run_workflow"}
WORKFLOW_SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1"}
WORKFLOW_STEM_TERMS = {"run", "train", "eval", "evaluate", "collect", "main", "cli"}
PATH_PATTERN_RE = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_./{}\[\]-]*\*[A-Za-z0-9_./{}\[\]-]*)(?![A-Za-z0-9_./-])")


def _norm_path(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _role_from_evidence_selection(block: dict[str, Any]) -> str | None:
    for item in block.get("evidence_selection") or []:
        text = str(item)
        prefix = "role_representative:"
        if text.startswith(prefix):
            return text[len(prefix):]
    return None


def _roles_from_context_item(item: dict[str, Any]) -> set[str]:
    roles: set[str] = set()
    role_scores = item.get("roles")
    if isinstance(role_scores, dict):
        for role, score in role_scores.items():
            try:
                if float(score) > 0:
                    roles.add(str(role))
            except (TypeError, ValueError):
                if score:
                    roles.add(str(role))

    explicit_role = _role_from_evidence_selection(item)
    if explicit_role:
        roles.add(explicit_role)

    for key in ("context_selection", "evidence_selection", "reasons"):
        for raw in item.get(key) or []:
            text = str(raw)
            for prefix in (
                "context_role_representative:",
                "role_representative:",
                "repo_analysis_role:",
                "repo_role:",
            ):
                if text.startswith(prefix):
                    roles.add(text[len(prefix):])
    return roles


def _append_role_paths_from_assignment(out: list[str], value: Any) -> None:
    if isinstance(value, str):
        path = _norm_path(value)
        if path and path not in out:
            out.append(path)
    elif isinstance(value, dict):
        path = _norm_path(value.get("path"))
        if path and path not in out:
            out.append(path)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _append_role_paths_from_assignment(out, item)


def _paths_for_context_role(context_pack: dict[str, Any] | None, role: str, analysis_context: dict[str, Any] | None = None) -> list[str]:
    pack = context_pack or {}
    out: list[str] = []
    for key in ("evidence_blocks", "selected_files"):
        for item in pack.get(key) or []:
            if not isinstance(item, dict):
                continue
            path = _norm_path(item.get("path"))
            if path and role in _roles_from_context_item(item) and path not in out:
                out.append(path)
    ctx = analysis_context or {}
    for source in (
        ((ctx.get("role_coverage_after") or {}).get("role_assignments") or {}).get(role),
        ((ctx.get("selection") or {}).get("role_assignments") or {}).get(role),
        ((ctx.get("compact_repo_map") or {}).get("candidates_by_role") or {}).get(role),
        ((ctx.get("repo_map") or {}).get("candidates_by_role") or {}).get(role),
    ):
        _append_role_paths_from_assignment(out, source)
    return out


def _path_suffix(path: str) -> str:
    return Path(path).suffix.lower()


def _path_stem_tokens(path: str) -> set[str]:
    stem = Path(path).stem.lower()
    return {token for token in re.split(r"[^a-z0-9]+", stem) if token}


def _workflow_path_like(path: str) -> bool:
    suffix = _path_suffix(path)
    if suffix in WORKFLOW_SCRIPT_SUFFIXES:
        return True
    return bool(_path_stem_tokens(path) & WORKFLOW_STEM_TERMS)


def _same_interchangeable_path_family(role: str, path: str, candidate: str) -> bool:
    if role != "run_workflow":
        return True
    suffix = _path_suffix(path)
    candidate_suffix = _path_suffix(candidate)
    if suffix in WORKFLOW_SCRIPT_SUFFIXES:
        return candidate_suffix in WORKFLOW_SCRIPT_SUFFIXES
    if suffix:
        return candidate_suffix == suffix and _workflow_path_like(candidate)
    return _workflow_path_like(candidate)


def _role_has_available_evidence(analysis_context: dict[str, Any], role: str) -> bool:
    if role == "tests":
        compact = analysis_context.get("compact_repo_map") or {}
        repo_map = analysis_context.get("repo_map") or {}
        if compact.get("has_tests") is False or repo_map.get("has_tests") is False:
            return False

    for source in (
        ((analysis_context.get("role_coverage_after") or {}).get("role_assignments") or {}).get(role),
        ((analysis_context.get("selection") or {}).get("role_assignments") or {}).get(role),
        ((analysis_context.get("compact_repo_map") or {}).get("candidates_by_role") or {}).get(role),
        ((analysis_context.get("repo_map") or {}).get("candidates_by_role") or {}).get(role),
    ):
        paths: list[str] = []
        _append_role_paths_from_assignment(paths, source)
        if paths:
            return True

    for item in analysis_context.get("selected_files") or []:
        if isinstance(item, dict) and role in _roles_from_context_item(item):
            return True
    return False


def _task_or_contract_mentions_role(analysis_context: dict[str, Any], role: str) -> bool:
    contract = analysis_context.get("analysis_contract") or {}
    haystack = " ".join(
        str(value)
        for value in (
            contract.get("task"),
            contract.get("objective"),
            analysis_context.get("task"),
            analysis_context.get("objective"),
            " ".join(str(q.get("question", "")) for q in contract.get("required_questions") or [] if isinstance(q, dict)),
        )
        if value
    ).lower()
    if role == "tests":
        return "测试" in haystack or bool(re.search(r"\b(?:test|tests|pytest|unittest)\b", haystack))
    return role in haystack


def _effective_missing_roles(analysis_context: dict[str, Any], missing_roles: list[str]) -> list[str]:
    effective: list[str] = []
    for role in missing_roles:
        role_name = str(role)
        if _role_has_available_evidence(analysis_context, role_name) or _task_or_contract_mentions_role(analysis_context, role_name):
            effective.append(role_name)
    return effective


def _effective_role_coverage_ratio(
    analysis_context: dict[str, Any],
    raw_coverage_ratio: float,
    raw_missing_roles: list[str],
) -> tuple[float, list[str]]:
    """Measure coverage against roles that the repository can actually supply.

    The repository map always uses the full cross-domain role taxonomy. Small or
    non-ML repositories legitimately have no evidence for many of those roles, so
    treating every absent role as part of the denominator makes a complete
    analysis look incomplete. Preserve the raw ratio for auditability, but gate on
    roles backed by repository evidence or explicitly requested by the task.
    """
    relevant_roles = [
        role
        for role in ROLES
        if _role_has_available_evidence(analysis_context, role)
        or _task_or_contract_mentions_role(analysis_context, role)
    ]
    if not relevant_roles:
        return raw_coverage_ratio, []

    missing = {str(role) for role in raw_missing_roles}
    covered_count = sum(role not in missing for role in relevant_roles)
    return round(covered_count / len(relevant_roles), 3), relevant_roles


def _alternate_path_hits(
    report: str,
    context_pack: dict[str, Any] | None,
    role: str,
    path: str,
    analysis_context: dict[str, Any] | None = None,
) -> list[str]:
    if role not in INTERCHANGEABLE_PATH_EVIDENCE_ROLES:
        return []
    return [
        candidate
        for candidate in _paths_for_context_role(context_pack, role, analysis_context)
        if candidate != path and candidate in report and _same_interchangeable_path_family(role, path, candidate)
    ]


def _path_pattern_hits(report: str, path: str) -> list[str]:
    hits: list[str] = []
    for raw in PATH_PATTERN_RE.findall(report):
        pattern = _norm_path(raw).strip("`'\".,;:()[]{}")
        if not pattern or "*" not in pattern:
            continue
        if fnmatch.fnmatch(path, pattern) and pattern not in hits:
            hits.append(pattern)
    return hits


def _symbol_terms(block: dict[str, Any]) -> list[str]:
    symbols = block.get("symbols") or {}
    out: list[str] = []
    for key in ("classes", "functions"):
        for value in symbols.get(key) or []:
            name = str(value).strip()
            if not name:
                continue
            out.append(name)
            if "(" in name:
                out.append(name.split("(", 1)[0])
    return _dedupe_terms(out, limit=20)


def _structured_field_terms(block: dict[str, Any]) -> list[str]:
    path = _norm_path(block.get("path")).lower()
    content = str(block.get("content") or "")
    if not path.endswith((".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini", ".cfg")):
        return []
    out = re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]{2,})[\"']\s*:", content)
    out.extend(
        re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:accuracy|acc|iou|mae|mse|loss|score|metric|model|config|precision|recall|f1|auc)[A-Za-z0-9_]*\b",
            content,
            flags=re.I,
        )
    )
    return _dedupe_terms(out, limit=30)


def _dedupe_terms(items: list[str], limit: int | None = None) -> list[str]:
    out: list[str] = []
    for item in items:
        value = str(item).strip()
        if len(value) < 3:
            continue
        if value not in out:
            out.append(value)
        if limit is not None and len(out) >= limit:
            break
    return out


def _term_hits(report: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term and term in report]


def _context_pack_report_view(context_pack: dict[str, Any] | None, max_chars: int = 30000) -> dict[str, Any]:
    pack = context_pack or {}
    blocks = []
    used = 0
    for block in pack.get("evidence_blocks") or []:
        content = str(block.get("content") or "")
        content_limit = 1200
        if used >= max_chars:
            break
        content = truncate(content, min(content_limit, max_chars - used))
        used += len(content)
        blocks.append({
            "path": block.get("path"),
            "role": _role_from_evidence_selection(block),
            "line_range": [block.get("start_line"), block.get("end_line")],
            "roles": block.get("roles", {}),
            "symbols": block.get("symbols", {}),
            "structured_fields": _structured_field_terms(block)[:20],
            "content": content,
        })
    return {
        "version": (pack or {}).get("version"),
        "evidence_diversity": (pack or {}).get("evidence_diversity"),
        "selected_files": (pack or {}).get("selected_files", [])[:24],
        "evidence_blocks": blocks,
        "instruction": (
            "For each core source evidence block, the report should cite the path and at least one concrete class/function. "
            "For structured result/config evidence, cite concrete field names."
        ),
    }


def _context_pack_evidence_quality(
    report: str,
    context_pack: dict[str, Any] | None,
    analysis_context: dict[str, Any] | None = None,
    required_path_roles: set[str] | None = None,
    required_symbol_roles: set[str] | None = None,
) -> dict[str, Any]:
    pack = context_pack or {}
    blocks = [b for b in (pack.get("evidence_blocks") or []) if isinstance(b, dict) and b.get("ok", True)]
    if not blocks:
        return {
            "version": "analysis_evidence_quality_v2",
            "ok": True,
            "warnings": [],
            "checked_blocks": 0,
            "path_required": [],
            "missing_path_mentions": [],
            "symbol_required": [],
            "missing_symbol_mentions": [],
            "structured_required": [],
            "missing_structured_field_mentions": [],
        }

    path_required: list[dict[str, Any]] = []
    missing_path_mentions: list[dict[str, Any]] = []
    symbol_required: list[dict[str, Any]] = []
    missing_symbol_mentions: list[dict[str, Any]] = []
    structured_required: list[dict[str, Any]] = []
    missing_structured_field_mentions: list[dict[str, Any]] = []

    for block in blocks:
        path = _norm_path(block.get("path"))
        role = _role_from_evidence_selection(block)
        fields = _structured_field_terms(block)
        structured_hits = _term_hits(report, fields)
        structured_min_hits = min(2, len(fields))
        if role in PATH_REQUIRED_EVIDENCE_ROLES and path and (required_path_roles is None or role in required_path_roles):
            item = {"role": role, "path": path}
            path_required.append(item)
            if path not in report:
                alternate_hits = _alternate_path_hits(report, pack, role, path, analysis_context)
                if alternate_hits:
                    item["satisfied_by_alternate_path"] = True
                    item["alternate_hits"] = alternate_hits[:8]
                else:
                    pattern_hits = _path_pattern_hits(report, path)
                    if pattern_hits:
                        item["satisfied_by_path_pattern"] = True
                        item["path_pattern_hits"] = pattern_hits[:8]
                    elif role == "config_or_arguments" and fields and len(structured_hits) >= structured_min_hits:
                        item["satisfied_by_structured_fields"] = True
                        item["structured_field_hits"] = structured_hits[:8]
                    else:
                        missing_path_mentions.append(item)

        symbols = _symbol_terms(block)
        if role in CORE_EVIDENCE_SYMBOL_ROLES and path and symbols and (required_symbol_roles is None or role in required_symbol_roles):
            hits = _term_hits(report, symbols)
            item = {"role": role, "path": path, "terms": symbols[:10], "hits": hits[:10]}
            symbol_required.append(item)
            if not hits:
                missing_symbol_mentions.append(item)

        if role in STRUCTURED_RESULT_ROLES and path and fields:
            item = {
                "role": role,
                "path": path,
                "terms": fields[:12],
                "hits": structured_hits[:12],
                "min_hits": structured_min_hits,
            }
            structured_required.append(item)
            if len(structured_hits) < structured_min_hits:
                missing_structured_field_mentions.append(item)

    warnings: list[str] = []
    if missing_path_mentions:
        warnings.append("report missing required evidence path mentions")
    if missing_symbol_mentions:
        warnings.append("report missing concrete class/function mentions from core evidence")
    if missing_structured_field_mentions:
        warnings.append("report missing structured field mentions from result/config evidence")

    return {
        "version": "analysis_evidence_quality_v2",
        "ok": not warnings,
        "warnings": warnings,
        "checked_blocks": len(blocks),
        "path_required": path_required,
        "missing_path_mentions": missing_path_mentions,
        "symbol_required": symbol_required,
        "missing_symbol_mentions": missing_symbol_mentions,
        "structured_required": structured_required,
        "missing_structured_field_mentions": missing_structured_field_mentions,
    }


def _augment_context_with_pack_paths(analysis_context: dict[str, Any], context_pack: dict[str, Any] | None) -> dict[str, Any]:
    pack = context_pack or analysis_context.get("context_pack")
    if not pack:
        return analysis_context
    paths = [
        _norm_path(block.get("path"))
        for block in (pack.get("evidence_blocks") or [])
        if isinstance(block, dict) and block.get("path")
    ]
    if not paths:
        return analysis_context
    out = dict(analysis_context)
    selected = list(out.get("selected_files") or [])
    for path in paths:
        if path and path not in selected:
            selected.append(path)
    out["selected_files"] = selected
    out["context_pack"] = pack
    return out


def _generic_quality(report: str, analysis_context: dict) -> dict:
    lower = report.lower()
    missing_headings = [h for h in REQUIRED_OVERVIEW_HEADINGS if not _section_present(report, h)]
    too_short = len(report) < 2500
    coverage = analysis_context.get("role_coverage_after") or {}
    raw_missing_roles = [str(role) for role in (coverage.get("missing_roles") or [])]
    missing_roles = _effective_missing_roles(analysis_context, raw_missing_roles)
    raw_coverage_ratio = float(coverage.get("coverage_ratio") or 0.0)
    coverage_ratio, relevant_roles = _effective_role_coverage_ratio(
        analysis_context,
        raw_coverage_ratio,
        raw_missing_roles,
    )
    selected_files = analysis_context.get("selected_files") or []
    evidence_paths = collect_analysis_evidence_paths(analysis_context)
    path_mentions = analysis_path_mentions(report, analysis_context)
    available_paths: list[str] = []
    for item in selected_files:
        path = _norm_path(item.get("path") if isinstance(item, dict) else item)
        if path and path not in available_paths:
            available_paths.append(path)
    for item in evidence_paths:
        path = _norm_path(item)
        if path and path not in available_paths:
            available_paths.append(path)
    target_path_mentions = max(3, min(8, len(selected_files) // 3))
    required_path_mentions = min(len(available_paths), target_path_mentions)
    weak_evidence = len(path_mentions) < required_path_mentions
    evidence_index = analysis_context.get("evidence_index") or {}
    evidence_terms = []
    for terms in (evidence_index.get("metric_or_domain_terms_by_file") or {}).values():
        evidence_terms.extend([str(t) for t in terms])
    for block in (analysis_context.get("context_pack") or {}).get("evidence_blocks") or []:
        if isinstance(block, dict):
            evidence_terms.extend(_structured_field_terms(block))
    unique_terms = []
    for t in evidence_terms:
        if t not in unique_terms:
            unique_terms.append(t)
    generic_metric_claims = []
    for term in ["precision", "recall", "f1-score", "f1 score", "auc"]:
        if term in lower and not any(term.replace("-", "_") in str(x).lower().replace("-", "_") for x in unique_terms):
            generic_metric_claims.append(term)
    evidence_term_mentions = [t for t in unique_terms if str(t) and str(t) in report]
    too_few_evidence_terms = bool(unique_terms and len(evidence_term_mentions) < min(3, len(unique_terms)))
    warnings: list[str] = []
    if missing_headings:
        warnings.append(f"missing headings: {missing_headings}")
    if too_short:
        warnings.append("report too short")
    if missing_roles:
        warnings.append(f"missing semantic roles: {missing_roles}")
    if coverage_ratio < 0.70:
        warnings.append(f"low role coverage: {coverage_ratio}")
    if weak_evidence:
        warnings.append("report cites too few selected evidence paths")
    if generic_metric_claims:
        warnings.append(f"generic metric claims not grounded in evidence: {generic_metric_claims}")
    if too_few_evidence_terms:
        warnings.append("report mentions too few concrete evidence terms from selected files")
    return {
        "ok": not missing_headings and not too_short and coverage_ratio >= 0.70 and not weak_evidence and not generic_metric_claims and not too_few_evidence_terms,
        "missing_headings": missing_headings,
        "too_short": too_short,
        "missing_roles": missing_roles,
        "raw_missing_roles": raw_missing_roles,
        "coverage_ratio": coverage_ratio,
        "raw_coverage_ratio": raw_coverage_ratio,
        "relevant_roles": relevant_roles,
        "selected_files": selected_files,
        "evidence_paths_count": len(evidence_paths),
        "available_paths_count": len(available_paths),
        "required_path_mentions": required_path_mentions,
        "path_mentions_count": len(path_mentions),
        "path_mentions": path_mentions[:60],
        "weak_evidence": weak_evidence,
        "evidence_terms_count": len(unique_terms),
        "evidence_term_mentions": evidence_term_mentions[:60],
        "too_few_evidence_terms": too_few_evidence_terms,
        "generic_metric_claims": generic_metric_claims,
        "chars": len(report),
        "warnings": warnings,
    }


def _required_symbol_roles_for_contract(report_type: str, analysis_contract: dict[str, Any]) -> set[str] | None:
    if report_type == "repository_overview":
        return set(CORE_EVIDENCE_SYMBOL_ROLES)
    question_ids = {
        str(q.get("id") or "")
        for q in analysis_contract.get("required_questions") or []
        if isinstance(q, dict) and q.get("required", True)
    }
    roles: set[str] = set()
    if "data_inputs" in question_ids:
        roles.add("data_pipeline")
    if "models" in question_ids:
        roles.add("model_definition")
    if "losses" in question_ids:
        roles.add("loss_definition")
    if "metrics_files" in question_ids or "aggregation_flow" in question_ids:
        roles.add("metric_evaluation")
    return roles


def _required_path_roles_for_contract(report_type: str, analysis_contract: dict[str, Any]) -> set[str] | None:
    if report_type == "repository_overview":
        return set(PATH_REQUIRED_EVIDENCE_ROLES)
    question_ids = {
        str(q.get("id") or "")
        for q in analysis_contract.get("required_questions") or []
        if isinstance(q, dict) and q.get("required", True)
    }
    roles: set[str] = set()
    if "data_inputs" in question_ids:
        roles.add("data_pipeline")
    if "models" in question_ids:
        roles.add("model_definition")
    if "losses" in question_ids:
        roles.add("loss_definition")
    if "metrics_files" in question_ids:
        roles.add("metric_evaluation")
    if "result_summary_files" in question_ids or "configs_experiment_matrix" in question_ids:
        roles.update({"results_or_outputs", "config_or_arguments"})
    if "entrypoints_workflow" in question_ids or "aggregation_flow" in question_ids:
        roles.update({"entrypoint", "run_workflow"})
    return roles


def _contract_relevant_evidence_paths(
    context_pack: dict[str, Any] | None,
    report_type: str,
    analysis_contract: dict[str, Any],
) -> list[str]:
    if report_type == "repository_overview":
        return []
    roles = _required_path_roles_for_contract(report_type, analysis_contract)
    if roles is None:
        return []
    out: list[str] = []
    for block in (context_pack or {}).get("evidence_blocks") or []:
        if not isinstance(block, dict):
            continue
        role = _role_from_evidence_selection(block)
        path = _norm_path(block.get("path"))
        if role in roles and path and path not in out:
            out.append(path)
    return out


def _quality(
    report: str,
    analysis_context: dict,
    analysis_contract: dict | None = None,
    context_pack: dict | None = None,
    section_coverage_review: dict[str, Any] | None = None,
) -> dict:
    analysis_context = _augment_context_with_pack_paths(analysis_context, context_pack)
    analysis_contract = analysis_contract or analysis_context.get("analysis_contract") or {}
    report_type = analysis_contract.get("report_type", "repository_overview")
    contract_context = analysis_context
    relevant_paths = _contract_relevant_evidence_paths(context_pack or analysis_context.get("context_pack"), report_type, analysis_contract)
    if relevant_paths:
        contract_context = dict(analysis_context)
        contract_context["contract_relevant_evidence_paths"] = relevant_paths
    generic = _generic_quality(report, analysis_context)
    contract_check = verify_analysis_report_against_contract(
        report,
        analysis_contract,
        contract_context,
        section_coverage_review=section_coverage_review,
    )
    evidence_quality = _context_pack_evidence_quality(
        report,
        context_pack or analysis_context.get("context_pack"),
        analysis_context,
        required_path_roles=_required_path_roles_for_contract(report_type, analysis_contract),
        required_symbol_roles=_required_symbol_roles_for_contract(report_type, analysis_contract),
    )
    language_quality = response_language_quality(analysis_contract.get("task") or "", report, artifact="final report")
    # For task-focused analysis, contract coverage is the main gate. For generic
    # overview, keep the old generic shape gate and add contract as extra signal.
    if report_type == "repository_overview":
        ok = generic["ok"] and contract_check["ok"] and evidence_quality["ok"] and language_quality["ok"]
        warnings = list(generic.get("warnings", []))
    else:
        # Task-focused reports can be shorter and need not include all overview headings,
        # but must answer the requested questions and cite evidence.
        ok = contract_check["ok"] and evidence_quality["ok"] and language_quality["ok"] and len(report) >= 1800 and not generic.get("too_few_evidence_terms") and not generic.get("generic_metric_claims")
        warnings = []
        if generic.get("coverage_ratio", 0.0) < 0.70:
            warnings.append(f"low role coverage: {generic.get('coverage_ratio')}")
        if generic.get("generic_metric_claims"):
            warnings.append(f"generic metric claims not grounded in evidence: {generic.get('generic_metric_claims')}")
        if generic.get("too_few_evidence_terms"):
            warnings.append("report mentions too few concrete evidence terms from selected files")
    warnings.extend(contract_check.get("failures", []))
    warnings.extend(evidence_quality.get("warnings", []))
    if language_quality.get("warning"):
        warnings.append(language_quality["warning"])
    if report_type != "repository_overview" and len(report) < 1800:
        warnings.append("task-focused report too short")
    advisory_warnings = [w for w in generic.get("warnings", []) if w not in warnings]
    out = dict(generic)
    out.update({
        "ok": ok,
        "report_type": report_type,
        "analysis_contract_check": contract_check,
        "evidence_quality": evidence_quality,
        "language_quality": language_quality,
        "warnings": warnings,
        "advisory_warnings": advisory_warnings,
    })
    if section_coverage_review:
        out["section_coverage_review"] = section_coverage_review
    return out


def _semantic_section_coverage_review(
    report: str,
    analysis_contract: dict[str, Any],
    contract_check: dict[str, Any],
    client: OpenAICompatClient,
    trace,
) -> dict[str, Any] | None:
    missing_sections = [str(s) for s in contract_check.get("missing_sections") or [] if str(s).strip()]
    headings = extract_report_headings(report)
    if not missing_sections or not headings:
        return None
    prompt = {
        "task": (
            "Decide whether existing Markdown headings in the report semantically cover the required sections. "
            "This review only checks heading/section equivalence. Do not waive evidence, path, field, or factual requirements."
        ),
        "required_missing_sections": missing_sections[:12],
        "all_required_sections": analysis_contract.get("required_sections", []),
        "report_headings": headings[:80],
        "report_excerpt": truncate(report, 12000),
        "output_schema": {
            "section_coverage": [
                {
                    "required_section": "exact required section name",
                    "covered": True,
                    "matched_heading": "exact heading from report, or null",
                    "reason": "short reason",
                }
            ]
        },
    }
    try:
        raw = client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a strict semantic reviewer for report section headings. "
                        "Return JSON only. Mark covered=true only when the existing report has a heading "
                        "or clearly labeled section with the same meaning as the required section."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
            ],
            purpose="analysis_section_coverage_review",
            max_tokens=900,
        )
        review = extract_json_object(raw)
        if not isinstance(review.get("section_coverage"), list):
            return None
        trace.event(
            "analysis_section_coverage_review",
            missing_sections=missing_sections,
            review=review,
        )
        return review
    except Exception as exc:
        trace.event("analysis_section_coverage_review_failed", error=str(exc), missing_sections=missing_sections)
        return None


def _quality_with_semantic_section_review(
    report: str,
    analysis_context: dict,
    analysis_contract: dict[str, Any],
    context_pack: dict | None,
    client: OpenAICompatClient,
    trace,
) -> dict:
    quality = _quality(report, analysis_context, analysis_contract, context_pack=context_pack)
    contract_check = quality.get("analysis_contract_check") or {}
    if not contract_check.get("missing_sections"):
        return quality
    review = _semantic_section_coverage_review(report, analysis_contract, contract_check, client, trace)
    if not review:
        return quality
    return _quality(
        report,
        analysis_context,
        analysis_contract,
        context_pack=context_pack,
        section_coverage_review=review,
    )


def _compact_context(analysis_context: dict, max_chars: int, context_pack: dict | None = None) -> str:
    compact = {
        "analysis_contract": analysis_context.get("analysis_contract", {}),
        "context_pack_evidence": _context_pack_report_view(context_pack or analysis_context.get("context_pack")),
        "structured_memory": analysis_context.get("structured_memory", {}),
        "project_types": analysis_context.get("project_types", []),
        "selection": analysis_context.get("selection", {}),
        "role_coverage_before": analysis_context.get("role_coverage_before", {}),
        "role_coverage_after": analysis_context.get("role_coverage_after", {}),
        "role_summaries": analysis_context.get("role_summaries", {}),
        "selected_files": analysis_context.get("selected_files", []),
        "context_budget": analysis_context.get("context_budget", {}),
        "evidence_index": analysis_context.get("evidence_index", {}),
        "project_memory_compact": analysis_context.get("project_memory_compact", ""),
        "read_result": analysis_context.get("read_result", {}),
        "compact_repo_map_summary": {
            "file_count": (analysis_context.get("compact_repo_map") or {}).get("file_count"),
            "py_file_count": (analysis_context.get("compact_repo_map") or {}).get("py_file_count"),
            "project_types": (analysis_context.get("compact_repo_map") or {}).get("project_types"),
            "project_type_signals": (analysis_context.get("compact_repo_map") or {}).get("project_type_signals"),
            "candidates_by_role": (analysis_context.get("compact_repo_map") or {}).get("candidates_by_role", {}),
        },
    }
    return truncate(json.dumps(compact, ensure_ascii=False, indent=2), max_chars)


def _report_instructions(contract: dict) -> str:
    if contract.get("report_type") in {"metric_result_summary", "script_design_analysis"}:
        return METRIC_RESULT_REPORT_INSTRUCTIONS + "\n" + REPORT_COMPLETENESS_CONSTRAINTS
    return OVERVIEW_REPORT_INSTRUCTIONS + "\n" + REPORT_COMPLETENESS_CONSTRAINTS


def _language_instruction(task: str) -> str:
    return language_instruction_for_text(task, artifact="final report")


def _quality_feedback_for_prompt(quality: dict[str, Any]) -> dict[str, Any]:
    evidence = quality.get("evidence_quality") or {}
    language = quality.get("language_quality") or {}
    return {
        "ok": quality.get("ok"),
        "warnings": quality.get("warnings", []),
        "language_quality": language,
        "missing_headings": quality.get("missing_headings", []),
        "analysis_contract_failures": (quality.get("analysis_contract_check") or {}).get("failures", []),
        "missing_evidence_paths": evidence.get("missing_path_mentions", [])[:12],
        "missing_core_symbols": evidence.get("missing_symbol_mentions", [])[:12],
        "missing_structured_fields": evidence.get("missing_structured_field_mentions", [])[:12],
        "required_action": (
            "Rewrite the full report. For every missing evidence item, cite the exact path and include at least one "
            "listed class/function/field. Keep code identifiers and file paths unchanged."
        ),
    }


def analyze_report_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("analyze_report_start")
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    repo_map = state.get("repo_map", {})
    analysis_context = state.get("repo_analysis_context", {})
    analysis_contract = state.get("analysis_contract") or analysis_context.get("analysis_contract") or build_analysis_contract(state.get("user_task") or state.get("task", ""), state.get("task_spec") or {})
    analysis_context["analysis_contract"] = analysis_contract
    analysis_context.setdefault("structured_memory", state.get("structured_memory") or {})
    context_pack = state.get("context_pack") or {}
    max_context_chars = client.analysis_report_context_chars
    report_max_tokens = client.analysis_report_max_tokens
    revision_attempts = max(0, client.analysis_report_revisions)
    compact_context = _compact_context(analysis_context, max_context_chars, context_pack=context_pack)
    project_memory_prompt = compact_project_memory_for_prompt(state.get("project_memory") or {}, max_chars=11000)
    questions = "\n".join([f"- {q.get('id')}: {q.get('question')}" for q in analysis_contract.get("required_questions", [])])
    runtime_instructions = str(state.get("task_runtime_instructions") or "").strip()
    runtime_block = f"Runtime UI guidance, lower priority than the user's task:\n{runtime_instructions}\n\n" if runtime_instructions else ""
    user = (
        f"Task:\n{state.get('task')}\n\n"
        f"{runtime_block}"
        f"Task spec:\n{json.dumps(state.get('task_spec'), ensure_ascii=False)}\n\n"
        f"Analysis contract. You MUST answer every required question and use the report shape below:\n{json.dumps(analysis_contract, ensure_ascii=False, indent=2)}\n\n"
        f"Required questions:\n{questions}\n\n"
        f"Repository map summary:\nfile_count={len(repo_map.get('files', []))}, "
        f"py_file_count={len(repo_map.get('py_files', []))}, project_types={repo_map.get('project_types', [])}\n\n"
        f"Long-term project memory from prior scans/runs, use as hints only:\n{project_memory_prompt}\n\n"
        f"Compact repository evidence with selected snippets, structured memory, role coverage and evidence index:\n{compact_context}\n\n"
        f"{_language_instruction(state.get('task') or '')} "
        "This is a read-only analysis task. Do not claim code was changed. "
        "Mention concrete file paths for evidence. For each core evidence source file, mention at least one concrete class/function from the provided symbols. "
        "For result/config JSON or CSV evidence, mention concrete field names. If evidence is missing, say uncertain and list what should be inspected next. "
        "Keep the report complete before it is detailed: include all required top-level sections even if some sections must be concise."
    )
    trace.event(
        "analyze_report_prompt_budget",
        user_chars=len(user),
        compact_context_chars=len(compact_context),
        report_max_tokens=report_max_tokens,
        coverage=analysis_context.get("role_coverage_after"),
        report_type=analysis_contract.get("report_type"),
    )
    report = client.chat([
        {"role": "system", "content": BASE_REPORT_SYSTEM + "\n\n" + _language_instruction(state.get("task") or "") + "\n\n" + _report_instructions(analysis_contract)},
        {"role": "user", "content": user},
    ], purpose="analyze_report", max_tokens=report_max_tokens)
    quality = _quality_with_semantic_section_review(
        report,
        analysis_context,
        analysis_contract,
        context_pack,
        client,
        trace,
    )
    revisions_used = 0
    for attempt in range(revision_attempts):
        if quality.get("ok"):
            break
        feedback = _quality_feedback_for_prompt(quality)
        trace.event("analyze_report_revision_start", attempt=attempt + 1, feedback=feedback)
        revision_user = (
            f"Task:\n{state.get('task')}\n\n"
            f"{runtime_block}"
            f"The previous analysis report failed the deterministic quality gate.\n"
            f"Quality feedback:\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n\n"
            f"Previous report:\n{truncate(report, 9000)}\n\n"
            f"Use the same evidence below and rewrite the full Markdown report, not a patch or explanation.\n"
            f"Compact repository evidence:\n{compact_context}\n\n"
            f"{_language_instruction(state.get('task') or '')} "
            "Every core role section must cite concrete paths and at least one class/function/field from the evidence. "
            "This revision must be complete and concise. Do not include long code blocks or long JSON snippets. "
            "Include every required top-level section before adding detail, especially any missing sections listed in the quality feedback."
        )
        report = client.chat([
            {"role": "system", "content": BASE_REPORT_SYSTEM + "\n\n" + _language_instruction(state.get("task") or "") + "\n\n" + _report_instructions(analysis_contract)},
            {"role": "user", "content": revision_user},
        ], purpose="analyze_report_revision", max_tokens=report_max_tokens)
        quality = _quality_with_semantic_section_review(
            report,
            analysis_context,
            analysis_contract,
            context_pack,
            client,
            trace,
        )
        revisions_used = attempt + 1
        trace.event("analyze_report_revision_done", attempt=attempt + 1, ok=quality.get("ok"), warnings=quality.get("warnings", []))
    quality["revision_attempts"] = revisions_used
    state["analysis_contract"] = analysis_contract
    state["analysis_report"] = report
    state["analysis_quality"] = quality
    Path(state["run_dir"]).mkdir(parents=True, exist_ok=True)
    Path(state["run_dir"], "analysis_report.md").write_text(report, encoding="utf-8")
    write_json(Path(state["run_dir"], "analysis_contract.json"), analysis_contract)
    write_json(Path(state["run_dir"], "analysis_contract_check.json"), quality.get("analysis_contract_check", {}))
    state["verification"] = VerificationResult(
        ok=quality["ok"],
        analysis_ok=quality["ok"],
        quality_warnings=quality.get("warnings", []),
    ).model_dump()
    state["stopped_reason"] = "analysis_complete" if quality["ok"] else "analysis_quality_failed"
    state["project_memory"] = update_project_memory_from_analysis(state)
    trace.event("analyze_report_done", quality=quality, report_path=str(Path(state["run_dir"], "analysis_report.md")), project_memory_path=state.get("project_profile_path"))
    trace.snapshot(state)
    return state
