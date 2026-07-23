from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.workspace.repo_map import compact_repo_map_for_llm
from coding_agent.tools.file_tools import BINARY_SUFFIXES
from coding_agent.core.utils import sha16, truncate, write_json


TEXT_CONTEXT_SUFFIXES = {
    ".py", ".pyi", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
    ".md", ".txt", ".rst", ".csv", ".tsv",
}
CONTEXT_PACK_VERSION = "context_pack_v2.1"
EVIDENCE_ROLE_ORDER = [
    "project_overview",
    "entrypoint",
    "data_pipeline",
    "model_definition",
    "loss_definition",
    "metric_evaluation",
    "results_or_outputs",
    "config_or_arguments",
    "run_workflow",
    "tests",
]
CODE_CONTEXT_ROLES = {
    "entrypoint",
    "data_pipeline",
    "model_definition",
    "loss_definition",
    "metric_evaluation",
    "run_workflow",
}
ROLE_KEYWORD_HINTS = {
    "entrypoint": {
        "main", "cli", "argparse", "click", "typer", "run", "command",
    },
    "data_pipeline": {
        "data", "dataset", "dataloader", "loader", "load", "read", "sample", "batch", "collate",
        "preprocess", "transform", "npz", "csv", "jsonl", "parquet",
    },
    "model_definition": {
        "model", "schema", "entity", "module", "component", "class", "builder",
    },
    "loss_definition": {
        "loss", "criterion", "objective", "cost", "penalty",
    },
    "metric_evaluation": {
        "metric", "measure", "score", "quality", "precision", "recall", "f1", "accuracy", "compute",
    },
    "run_workflow": {
        "run", "execute", "collect", "workflow", "pipeline", "python", "pytest", "bash", "sh",
    },
    "config_or_arguments": {
        "config", "argument", "args", "option", "parameter", "yaml", "json", "env",
    },
    "results_or_outputs": {
        "result", "summary", "metric", "score", "report", "output", "artifact", "json", "csv",
    },
    "tests": {
        "test", "pytest", "unittest", "assert", "fixture",
    },
    "project_overview": {
        "readme", "overview", "architecture", "project", "guide", "doc",
    },
}
STRUCTURED_CONTEXT_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
PINNED_EVIDENCE_REASONS = {
    "active_failure_target",
    "failure_issue_target",
    "changed_file",
    "agent_generated_file",
    "last_tool_result_path",
}
PINNED_CONTEXT_REASONS = PINNED_EVIDENCE_REASONS | {"planned_artifact"}
ROLE_ASSIGNMENT_SCORE = 70
REPO_SELECTED_SCORE = 32


@dataclass(frozen=True)
class ContextBudget:
    max_chars: int = 28000
    task_chars: int = 3500
    repo_chars: int = 6500
    memory_chars: int = 4500
    evidence_chars: int = 11000
    failure_chars: int = 4500
    history_chars: int = 3500

    @classmethod
    def from_max_chars(cls, max_chars: int = 28000) -> "ContextBudget":
        max_chars = max(8000, int(max_chars))
        return cls(
            max_chars=max_chars,
            task_chars=max(1200, int(max_chars * 0.12)),
            repo_chars=max(2200, int(max_chars * 0.22)),
            memory_chars=max(1200, int(max_chars * 0.14)),
            evidence_chars=max(3000, int(max_chars * 0.40)),
            failure_chars=max(1200, int(max_chars * 0.16)),
            history_chars=max(1000, int(max_chars * 0.12)),
        )

    def model_dump(self) -> dict[str, int]:
        return {
            "max_chars": self.max_chars,
            "task_chars": self.task_chars,
            "repo_chars": self.repo_chars,
            "memory_chars": self.memory_chars,
            "evidence_chars": self.evidence_chars,
            "failure_chars": self.failure_chars,
            "history_chars": self.history_chars,
        }


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _json_preview(value: Any, max_chars: int) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    if len(text) <= max_chars:
        return value
    return {"truncated": True, "preview": truncate(text, max_chars)}


def _hard_truncate(text: str, limit: int) -> str:
    """Return text whose length is at most limit, including the truncation marker."""
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    marker = f"\n...<truncated {len(text) - limit} chars>"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _add_candidate(scored: dict[str, dict[str, Any]], path: str | None, score: int, reason: str) -> None:
    rel = _norm(path)
    if not rel or rel.startswith(".coding_agent/"):
        return
    item = scored.setdefault(rel, {"path": rel, "score": 0, "reasons": []})
    item["score"] = int(item.get("score", 0)) + score
    if reason and reason not in item["reasons"]:
        item["reasons"].append(reason)


def _iter_repo_analysis_selected_files(ctx: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("selected_files", "focus_files"):
        for path in ctx.get(key) or []:
            if isinstance(path, str):
                paths.append(path)
    selection = ctx.get("selection") or {}
    if isinstance(selection, dict):
        for key in ("selected_files", "focus_files"):
            for path in selection.get(key) or []:
                if isinstance(path, str):
                    paths.append(path)
    return list(dict.fromkeys(_norm(p) for p in paths if _norm(p)))


def _iter_repo_analysis_role_assignments(ctx: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    containers = [ctx]
    selection = ctx.get("selection") or {}
    if isinstance(selection, dict):
        containers.append(selection)
    for container in containers:
        raw = container.get("role_assignments") or {}
        if not isinstance(raw, dict):
            continue
        for role, paths in raw.items():
            if not isinstance(paths, list):
                continue
            for path in paths:
                if isinstance(path, str) and _norm(path):
                    out.append((str(role), _norm(path)))
    return list(dict.fromkeys(out))


def _artifact_record_for_context(state: dict[str, Any], rel: str | None) -> dict[str, Any]:
    rel = _norm(rel)
    artifacts = (state.get("artifact_provenance") or {}).get("artifacts") or {}
    rec = artifacts.get(rel)
    if isinstance(rec, dict):
        return rec
    for path, value in artifacts.items():
        if _norm(path) == rel and isinstance(value, dict):
            return value
    return {}


def _is_prior_agent_artifact(item: dict[str, Any]) -> bool:
    origin = str(item.get("artifact_origin") or "")
    if not origin.startswith("agent_"):
        return False
    current_reasons = {"agent_generated_file", "changed_file", "active_failure_target", "failure_issue_target"}
    return not bool(set(item.get("reasons") or []) & current_reasons)


def _symbol_text(item: dict[str, Any]) -> str:
    symbols = item.get("symbols") or {}
    parts: list[str] = [_norm(item.get("path"))]
    for key in ("classes", "functions"):
        values = symbols.get(key) or []
        if isinstance(values, list):
            parts.extend(str(v) for v in values)
    return " ".join(parts).lower()


def _keyword_hit_count(item: dict[str, Any], role: str) -> int:
    text = _symbol_text(item)
    hints = ROLE_KEYWORD_HINTS.get(role) or set()
    return sum(1 for hint in hints if hint in text)


def _code_role_breadth(item: dict[str, Any]) -> int:
    return sum(1 for role in CODE_CONTEXT_ROLES if _role_score(item, role) > 0)


def _role_is_primary(item: dict[str, Any], role: str) -> bool:
    score = _role_score(item, role)
    if score <= 0:
        return False
    return score >= max((_role_score(item, r) for r in (item.get("roles") or {})), default=score)


def _has_explicit_role_assignment(item: dict[str, Any], role: str) -> bool:
    return f"repo_analysis_role:{role}" in set(item.get("reasons") or [])


def _eligible_role_representative(item: dict[str, Any], role: str) -> bool:
    score = _role_score(item, role)
    if score <= 0:
        return False
    if _has_explicit_role_assignment(item, role):
        return True
    if role in CODE_CONTEXT_ROLES:
        return score >= 35
    return score >= 30


def _repo_record_by_path(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_norm(str(r.get("path"))): r for r in (state.get("repo_map") or {}).get("records", []) if r.get("path")}


def select_context_files(state: dict[str, Any], max_files: int = 18) -> list[dict[str, Any]]:
    scored: dict[str, dict[str, Any]] = {}
    repo = state.get("repo_map") or {}
    records = _repo_record_by_path(state)
    repo_analysis_context = state.get("repo_analysis_context") or {}

    for path in (state.get("relevant_context") or {}).get("matched_files") or []:
        _add_candidate(scored, path, 50, "task_term_retrieval")
    for path in (state.get("relevant_context") or {}).get("memory_matched_files") or []:
        _add_candidate(scored, path, 45, "project_memory_retrieval")
    for path in _iter_repo_analysis_selected_files(repo_analysis_context):
        _add_candidate(scored, path, REPO_SELECTED_SCORE, "repo_analysis_selected")
    for role, path in _iter_repo_analysis_role_assignments(repo_analysis_context):
        score = ROLE_ASSIGNMENT_SCORE if role in EVIDENCE_ROLE_ORDER else max(40, ROLE_ASSIGNMENT_SCORE // 2)
        _add_candidate(scored, path, score, f"repo_analysis_role:{role}")
    for item in (state.get("file_plan") or {}).get("files") or []:
        if isinstance(item, dict):
            _add_candidate(scored, item.get("path"), 38, "planned_artifact")
    for item in state.get("generated_files") or []:
        if isinstance(item, dict):
            _add_candidate(scored, item.get("path"), 65, "agent_generated_file")
    for path in state.get("changed_files") or []:
        _add_candidate(scored, path, 70, "changed_file")

    failure = state.get("failure") or {}
    _add_candidate(scored, failure.get("target_file"), 110, "active_failure_target")
    for issue in (state.get("failure_issues") or []) + (state.get("traceback_issues") or []):
        if isinstance(issue, dict):
            _add_candidate(scored, issue.get("file") or issue.get("target_file") or issue.get("test_file"), 95, "failure_issue_target")

    last_data = (state.get("last_tool_result") or {}).get("data") or {}
    _add_candidate(scored, last_data.get("path"), 65, "last_tool_result_path")

    for rec in repo.get("top_records", [])[:80]:
        path = rec.get("path")
        if not path:
            continue
        score = max(5, min(35, int(rec.get("importance_score", 0) or 0) // 3))
        _add_candidate(scored, path, score, "repo_map_importance")

    for role, items in (repo.get("candidates_by_role") or {}).items():
        for item in items[:2]:
            _add_candidate(scored, item.get("path"), 18, f"repo_role:{role}")

    root = Path(state.get("workspace", ".")).resolve()
    ranked = []
    for rel, item in scored.items():
        p = (root / rel).resolve()
        if not str(p).startswith(str(root)) or not p.is_file():
            continue
        if p.suffix.lower() in BINARY_SUFFIXES:
            continue
        rec = records.get(rel) or {}
        origin = _artifact_record_for_context(state, rel).get("origin")
        score = int(item["score"])
        reasons = list(item["reasons"])
        if origin and str(origin).startswith("agent_") and not (set(reasons) & PINNED_CONTEXT_REASONS):
            score -= 45
            if "prior_agent_artifact_penalty" not in reasons:
                reasons.append("prior_agent_artifact_penalty")
        ranked.append({
            "path": rel,
            "score": score,
            "reasons": reasons,
            "artifact_origin": origin,
            "roles": rec.get("roles", {}),
            "symbols": {
                "classes": (rec.get("symbols") or {}).get("classes", [])[:10],
                "functions": (rec.get("symbols") or {}).get("functions", [])[:16],
                "imports": (rec.get("symbols") or {}).get("imports", [])[:16],
            },
        })
    return _rank_selected_context_files(ranked, max_files=max_files)


def _read_evidence(path: Path, max_chars: int) -> tuple[str, int, int, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    out: list[str] = []
    used = 0
    end_line = 0
    for idx, line in enumerate(lines, start=1):
        rendered = f"{idx}: {line}"
        if used + len(rendered) + 1 > max_chars and out:
            break
        out.append(rendered)
        used += len(rendered) + 1
        end_line = idx
    return "\n".join(out), 1 if out else 0, end_line, len(lines)


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _role_score(item: dict[str, Any], role: str) -> int:
    return _int_value((item.get("roles") or {}).get(role), 0)


def _item_roles(item: dict[str, Any]) -> list[str]:
    roles = item.get("roles") or {}
    return [
        role
        for role, score in sorted(roles.items(), key=lambda kv: _int_value(kv[1]), reverse=True)
        if _int_value(score) > 0
    ]


def _path_cluster(rel: str | None) -> str:
    rel = _norm(rel).lower()
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return rel
    suffix = Path(parts[-1]).suffix.lower()
    if len(parts) >= 3 and suffix in STRUCTURED_CONTEXT_SUFFIXES:
        return "/".join([parts[0], "*", parts[-1]])
    normalized = [re.sub(r"\d+", "{n}", part) for part in parts]
    if len(normalized) >= 4:
        return "/".join([normalized[0], normalized[1], "...", normalized[-1]])
    return "/".join(normalized)


def _is_pinned_evidence(item: dict[str, Any]) -> bool:
    return bool(set(item.get("reasons") or []) & PINNED_EVIDENCE_REASONS)


def _role_fit_score(item: dict[str, Any], role: str) -> int:
    rel = _norm(item.get("path"))
    suffix = Path(rel).suffix.lower()
    name = Path(rel).name.lower()
    score = _int_value(item.get("score"), 0)
    fit = _role_score(item, role) * 4 + min(score, 120)
    if _is_prior_agent_artifact(item):
        fit -= 80
    keyword_hits = _keyword_hit_count(item, role)
    if keyword_hits:
        fit += min(90, 28 + keyword_hits * 14)
    if _role_is_primary(item, role):
        fit += 25

    if role in CODE_CONTEXT_ROLES:
        if suffix == ".py":
            fit += 70
        elif suffix in {".sh", ".bash", ".zsh"} and role in {"entrypoint", "run_workflow"}:
            fit += 45
        elif suffix in STRUCTURED_CONTEXT_SUFFIXES:
            fit -= 45
        elif suffix in {".md", ".rst", ".txt"}:
            fit -= 20
        if suffix == ".py" and role not in {"entrypoint", "run_workflow"}:
            breadth = _code_role_breadth(item)
            if breadth > 2:
                fit -= (breadth - 2) * 18
            if any(reason == f"repo_analysis_role:{role}" for reason in item.get("reasons") or []):
                fit += 35
    elif role == "project_overview":
        if name.startswith("readme") or suffix in {".md", ".rst", ".txt"}:
            fit += 50
    elif role in {"results_or_outputs", "config_or_arguments"}:
        if suffix in STRUCTURED_CONTEXT_SUFFIXES:
            fit += 45
        if suffix == ".py" and role == "results_or_outputs":
            fit -= 20
    elif role == "tests":
        if suffix == ".py":
            fit += 35
    return fit


def _rank_selected_context_files(ranked: list[dict[str, Any]], max_files: int) -> list[dict[str, Any]]:
    if not ranked:
        return []
    max_files = max(1, int(max_files))
    cluster_cap = max(1, _int_value(os.getenv("AGENT_CONTEXT_SELECTED_CLUSTER_CAP"), 4))
    ordered: list[dict[str, Any]] = []
    used_paths: set[str] = set()
    cluster_counts: dict[str, int] = {}

    def append_item(item: dict[str, Any], reason: str, *, bypass_cluster: bool = False) -> bool:
        if len(ordered) >= max_files:
            return False
        rel = _norm(item.get("path"))
        if not rel or rel in used_paths:
            return False
        cluster = _path_cluster(rel)
        if not bypass_cluster and cluster_counts.get(cluster, 0) >= cluster_cap:
            return False
        planned = dict(item)
        selection = list(planned.get("context_selection") or [])
        if reason not in selection:
            selection.append(reason)
        planned["context_selection"] = selection
        planned["context_cluster"] = cluster
        ordered.append(planned)
        used_paths.add(rel)
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        return True

    ranked_by_score = sorted(ranked, key=lambda x: (_int_value(x.get("score")), _norm(x.get("path"))), reverse=True)
    for item in [x for x in ranked_by_score if set(x.get("reasons") or []) & PINNED_CONTEXT_REASONS]:
        append_item(item, "pinned_context", bypass_cluster=True)

    role_order = [role for role in EVIDENCE_ROLE_ORDER if any(_eligible_role_representative(item, role) for item in ranked)]
    for role in role_order:
        if len(ordered) >= max_files:
            break
        candidates = [x for x in ranked if _eligible_role_representative(x, role) and _norm(x.get("path")) not in used_paths]
        candidates.sort(key=lambda x: (_role_fit_score(x, role), _int_value(x.get("score")), _norm(x.get("path"))), reverse=True)
        for item in candidates:
            if append_item(item, f"context_role_representative:{role}"):
                break

    for item in ranked_by_score:
        append_item(item, "score_fill")

    if len(ordered) < min(max_files, len(ranked)):
        for item in ranked_by_score:
            append_item(item, "score_fill_over_cluster_minimum", bypass_cluster=True)
            if len(ordered) >= max_files:
                break
    return ordered[:max_files]


def build_evidence_plan(
    state: dict[str, Any],
    selected_files: list[dict[str, Any]],
    max_blocks: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not selected_files:
        return [], {
            "version": "role_quota_v1",
            "max_blocks": 0,
            "cluster_cap": 0,
            "covered_roles": [],
            "cluster_counts": {},
            "selected_paths": [],
        }

    env_max = os.getenv("AGENT_CONTEXT_PACK_MAX_EVIDENCE_BLOCKS")
    if max_blocks is None:
        max_blocks = _int_value(env_max, 14) if env_max else 14
    max_blocks = max(1, min(int(max_blocks), len(selected_files)))
    cluster_cap = max(1, _int_value(os.getenv("AGENT_CONTEXT_PACK_CLUSTER_CAP"), 2))
    role_order = [role for role in EVIDENCE_ROLE_ORDER if any(_eligible_role_representative(item, role) for item in selected_files)]

    ordered: list[dict[str, Any]] = []
    used_paths: set[str] = set()
    cluster_counts: dict[str, int] = {}
    covered_roles: set[str] = set()

    def append_item(item: dict[str, Any], reason: str, *, bypass_cluster: bool = False) -> bool:
        if len(ordered) >= max_blocks:
            return False
        rel = _norm(item.get("path"))
        if not rel or rel in used_paths:
            return False
        cluster = _path_cluster(rel)
        if not bypass_cluster and cluster_counts.get(cluster, 0) >= cluster_cap:
            return False
        planned = dict(item)
        selection = list(planned.get("evidence_selection") or [])
        if reason not in selection:
            selection.append(reason)
        planned["evidence_selection"] = selection
        planned["evidence_cluster"] = cluster
        ordered.append(planned)
        used_paths.add(rel)
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        covered_roles.update(_item_roles(planned))
        return True

    ranked = sorted(selected_files, key=lambda x: (_int_value(x.get("score")), _norm(x.get("path"))), reverse=True)
    for item in [x for x in ranked if _is_pinned_evidence(x)]:
        append_item(item, "pinned_failure_or_change", bypass_cluster=True)

    for role in role_order:
        if len(ordered) >= max_blocks:
            break
        candidates = [x for x in selected_files if _eligible_role_representative(x, role) and _norm(x.get("path")) not in used_paths]
        candidates.sort(key=lambda x: (_role_fit_score(x, role), _int_value(x.get("score")), _norm(x.get("path"))), reverse=True)
        for item in candidates:
            if append_item(item, f"role_representative:{role}"):
                break

    for item in ranked:
        append_item(item, "score_fill")

    minimum_blocks = min(max_blocks, len(selected_files), 4)
    if len(ordered) < minimum_blocks:
        for item in ranked:
            append_item(item, "score_fill_over_cluster_minimum", bypass_cluster=True)
            if len(ordered) >= minimum_blocks:
                break

    diversity = {
        "version": "role_quota_v1",
        "max_blocks": max_blocks,
        "cluster_cap": cluster_cap,
        "role_order": role_order,
        "covered_roles": sorted(covered_roles),
        "cluster_counts": dict(sorted(cluster_counts.items())),
        "selected_paths": [_norm(item.get("path")) for item in ordered],
        "pinned_count": sum(1 for item in ordered if "pinned_failure_or_change" in (item.get("evidence_selection") or [])),
    }
    return ordered, diversity


def _materialize_evidence_blocks(state: dict[str, Any], planned_files: list[dict[str, Any]], budget: ContextBudget) -> list[dict[str, Any]]:
    root = Path(state.get("workspace", ".")).resolve()
    blocks: list[dict[str, Any]] = []
    if not planned_files:
        return blocks
    per_file = max(900, min(2600, budget.evidence_chars // max(1, min(len(planned_files), 10))))
    used = 0
    for item in planned_files:
        rel = item.get("path")
        if not rel:
            continue
        p = (root / rel).resolve()
        if not str(p).startswith(str(root)) or not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in BINARY_SUFFIXES or (suffix and suffix not in TEXT_CONTEXT_SUFFIXES):
            continue
        remaining = budget.evidence_chars - used
        if remaining < 500:
            break
        limit = min(per_file, remaining)
        try:
            content, start_line, end_line, total_lines = _read_evidence(p, limit)
        except Exception as e:
            blocks.append({
                "path": rel,
                "ok": False,
                "reason": item.get("reasons", []),
                "error": str(e)[:500],
            })
            continue
        if not content:
            continue
        used += len(content)
        blocks.append({
            "path": rel,
            "ok": True,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "sha16": sha16(p.read_bytes()),
            "priority": item.get("score", 0),
            "reason": item.get("reasons", []),
            "evidence_selection": item.get("evidence_selection", []),
            "evidence_cluster": item.get("evidence_cluster"),
            "roles": item.get("roles", {}),
            "symbols": item.get("symbols", {}),
            "content": content,
        })
    return blocks


def build_evidence_blocks(state: dict[str, Any], selected_files: list[dict[str, Any]], budget: ContextBudget) -> list[dict[str, Any]]:
    planned_files, _ = build_evidence_plan(state, selected_files)
    return _materialize_evidence_blocks(state, planned_files, budget)


def build_context_pack(state: dict[str, Any], max_chars: int = 28000) -> dict[str, Any]:
    env_max = os.getenv("AGENT_CONTEXT_PACK_MAX_CHARS")
    if env_max:
        try:
            max_chars = int(env_max)
        except ValueError:
            pass
    budget = ContextBudget.from_max_chars(max_chars)
    selected_files = select_context_files(state, max_files=int(os.getenv("AGENT_CONTEXT_PACK_MAX_FILES", "18")))
    planned_files, evidence_diversity = build_evidence_plan(state, selected_files)
    evidence_blocks = _materialize_evidence_blocks(state, planned_files, budget)
    repo_overview = compact_repo_map_for_llm(state.get("repo_map") or {}, max_records=80) if state.get("repo_map") else {}
    pack = {
        "version": CONTEXT_PACK_VERSION,
        "budget": budget.model_dump(),
        "task": {
            "text": truncate(str(state.get("task", "")), budget.task_chars),
            "mode": state.get("mode"),
            "read_only": state.get("read_only"),
            "write_locked": state.get("write_locked"),
            "round_idx": state.get("round_idx"),
            "task_spec": _json_preview(state.get("task_spec") or {}, max(800, budget.task_chars // 3)),
            "task_intent": _json_preview(state.get("task_intent") or {}, max(1200, budget.task_chars // 2)),
        },
        "repo_overview": _json_preview(repo_overview, budget.repo_chars),
        "selected_files": selected_files,
        "evidence_diversity": evidence_diversity,
        "evidence_blocks": evidence_blocks,
        "memory_context": _json_preview({
            "memory_context": state.get("memory_context") or {},
            "project_memory_compact": (state.get("relevant_context") or {}).get("project_memory_compact"),
        }, budget.memory_chars),
        "schema_context": _json_preview({
            "analysis_contract": state.get("analysis_contract"),
            "interface_check": state.get("interface_check"),
        }, budget.memory_chars),
        "failure_context": _json_preview({
            "failure": state.get("failure"),
            "failure_issues": state.get("failure_issues"),
            "traceback_issues": state.get("traceback_issues"),
            "verification": state.get("verification"),
            "test_results": state.get("test_results"),
            "last_tool_result": state.get("last_tool_result"),
        }, budget.failure_chars),
        "recent_activity": _json_preview({
            "action_history_tail": (state.get("action_history") or [])[-10:],
            "repair_history_tail": (state.get("repair_history") or [])[-8:],
            "observations_tail": (state.get("observations") or [])[-6:],
        }, budget.history_chars),
        "compression_notes": [
            "context_pack_v2.1 uses deterministic file scoring, role quotas, cluster caps, and section budgets",
            "evidence_blocks include path, line range, reason, priority, and sha16",
            "role quotas are semantic-role based and do not depend on project-specific filenames",
            "llm_client still performs final hard truncation as a safety fallback",
        ],
    }
    rendered = render_context_pack_markdown(pack, max_chars=budget.max_chars)
    pack["budget"]["rendered_chars"] = len(rendered)
    pack["budget"]["evidence_block_count"] = len(evidence_blocks)
    return pack


def render_context_pack_markdown(pack: dict[str, Any], max_chars: int | None = None) -> str:
    budget = pack.get("budget") or {}
    limit = int(max_chars or budget.get("max_chars") or 28000)
    parts: list[str] = [
        "# Coding Agent Context Pack v2.1",
        f"version={pack.get('version')} max_chars={budget.get('max_chars')} rendered_budget={limit}",
        "\n## Task",
        json.dumps(pack.get("task", {}), ensure_ascii=False, indent=2, default=str),
        "\n## Repository Overview",
        truncate(json.dumps(pack.get("repo_overview", {}), ensure_ascii=False, indent=2, default=str), int(budget.get("repo_chars", 6500))),
        "\n## Selected Files",
    ]
    for item in pack.get("selected_files") or []:
        parts.append(
            f"- {item.get('path')} score={item.get('score')} reasons={item.get('reasons')} "
            f"context_selection={item.get('context_selection')} artifact_origin={item.get('artifact_origin')} "
            f"roles={item.get('roles')}"
        )
    parts.append("\n## Evidence Diversity")
    parts.append(json.dumps(pack.get("evidence_diversity", {}), ensure_ascii=False, indent=2, default=str))
    parts.append("\n## Evidence Blocks")
    for block in pack.get("evidence_blocks") or []:
        if not block.get("ok", True):
            parts.append(f"\n### {block.get('path')} [read failed]\n{block.get('error')}")
            continue
        parts.append(
            f"\n### {block.get('path')}:{block.get('start_line')}-{block.get('end_line')}"
            f"\nreason={block.get('reason')} priority={block.get('priority')} sha16={block.get('sha16')}"
            f"\nroles={block.get('roles')} symbols={block.get('symbols')}\n"
            + str(block.get("content", ""))
        )
    for title, key in [
        ("Memory Context", "memory_context"),
        ("Schema Context", "schema_context"),
        ("Failure Context", "failure_context"),
        ("Recent Activity", "recent_activity"),
        ("Compression Notes", "compression_notes"),
    ]:
        parts.append(f"\n## {title}")
        parts.append(json.dumps(pack.get(key), ensure_ascii=False, indent=2, default=str))
    return _hard_truncate("\n".join(parts), limit)


def write_context_pack(path: str | Path, state: dict[str, Any], max_chars: int = 28000) -> dict[str, Any]:
    pack = build_context_pack(state, max_chars=max_chars)
    write_json(path, pack)
    return pack
