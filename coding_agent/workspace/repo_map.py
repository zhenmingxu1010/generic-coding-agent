from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

from coding_agent.tools.file_tools import SKIP_DIRS, BINARY_SUFFIXES
from coding_agent.core.utils import truncate

TEXT_SUFFIXES = {
    ".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".md", ".txt", ".rst", ".csv",
}

ROLES = [
    "project_overview",
    "entrypoint",
    "data_pipeline",
    "model_definition",
    "loss_definition",
    "metric_evaluation",
    "run_workflow",
    "config_or_arguments",
    "results_or_outputs",
    "tests",
]


def _safe_read(path: Path, max_chars: int = 120000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def _walk(root: Path, max_files: int = 20000) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            rel = str((Path(dirpath) / name).relative_to(root))
            out.append(rel)
            if len(out) >= max_files:
                return sorted(out)
    return sorted(out)


def _python_ast_summary(text: str) -> dict[str, Any]:
    summary = {"imports": [], "classes": [], "functions": [], "parse_ok": False}
    try:
        tree = ast.parse(text)
    except Exception as e:
        summary["parse_error"] = str(e)[:300]
        return summary
    summary["parse_ok"] = True
    imports: list[str] = []
    classes: list[str] = []
    functions: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
        elif isinstance(node, ast.ClassDef):
            bases = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
            classes.append(node.name + (f"({','.join(bases[:3])})" if bases else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
    summary["imports"] = sorted(set(imports))[:80]
    summary["classes"] = classes[:80]
    summary["functions"] = functions[:120]
    return summary


def _base_signals(rel: str, text: str, size: int) -> dict[str, Any]:
    low_path = rel.lower()
    low = text.lower()
    suffix = Path(rel).suffix.lower()
    name = Path(rel).name.lower()
    return {
        "suffix": suffix,
        "size": size,
        "is_doc": suffix in {".md", ".rst", ".txt"} or name.startswith("readme"),
        "is_python": suffix == ".py",
        "is_shell": suffix in {".sh", ".bash", ".zsh"},
        "is_config_like": suffix in {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"},
        "is_result_like": suffix in {".json", ".csv", ".txt", ".md"} and bool(re.search(r"metric|result|summary|report|score|eval|acc|iou|mae|loss", low_path)),
        "has_main_guard": "if __name__" in text and "__main__" in text,
        "has_argparse": "argparse" in low or "add_argument" in low,
        "has_click_typer": "click." in low or "typer." in low or "from typer" in low or "import typer" in low,
        "mentions_train": bool(re.search(r"\b(train|fit|trainer|epoch|optimizer|backward)\b", low)),
        "mentions_eval": bool(re.search(r"\b(eval|evaluate|validation|test|metric|score)\b", low)),
        "mentions_data": bool(re.search(r"\b(dataset|dataloader|load_data|read_|npz|csv|jsonl|parquet|hdf|h5|sample)\b", low)),
        "mentions_model": bool(re.search(r"\b(model|module|network|backbone|transformer|cnn|resnet|mlp)\b", low)),
        "mentions_loss": bool(re.search(r"\b(loss|criterion|objective|bce|focal|cross_entropy|nll)\b", low)),
        "mentions_metrics": bool(re.search(r"\b(metric|accuracy|precision|recall|f1|iou|mae|mse|auc|score)\b", low)),
        "mentions_config": bool(re.search(r"\b(config|yaml|json|argparse|hydra|omegaconf|env|export|parameter|option)\b", low)),
        "calls_python": "python " in low or "python3 " in low or "torchrun" in low,
        "calls_pytest": "pytest" in low,
    }


def _add_python_signals(signals: dict[str, Any], py: dict[str, Any], text: str) -> None:
    imports = set(py.get("imports") or [])
    classes = "\n".join(py.get("classes") or []).lower()
    funcs = "\n".join(py.get("functions") or []).lower()
    low = text.lower()
    signals.update({
        "imports_torch": "torch" in imports or "pytorch_lightning" in imports,
        "imports_numpy_pandas": bool({"numpy", "pandas"} & imports),
        "imports_sklearn": "sklearn" in imports,
        "defines_dataset": "dataset" in classes or "dataloader" in classes or "dataset" in funcs,
        "defines_model": any(k in classes for k in ["module", "model", "network", "transformer", "resnet", "cnn", "mlp"]),
        "defines_loss": "loss" in classes or "loss" in funcs or "criterion" in funcs,
        "defines_metric": "metric" in classes or "metric" in funcs or "accuracy" in funcs or "iou" in funcs or "mae" in funcs,
        "defines_train_loop": any(k in funcs for k in ["train", "train_one_epoch", "fit", "main"] ) and bool(re.search(r"optimizer|backward|epoch|scheduler", low)),
        "defines_eval_loop": any(k in funcs for k in ["eval", "evaluate", "test", "validate"]),
    })


def _role_scores(rel: str, signals: dict[str, Any]) -> dict[str, int]:
    p = rel.lower()
    name = Path(rel).name.lower()
    s = {role: 0 for role in ROLES}
    if signals.get("is_doc"):
        s["project_overview"] += 50
    if name.startswith("readme") or "overview" in p or "architecture" in p:
        s["project_overview"] += 70
    if signals.get("has_main_guard") or signals.get("has_argparse") or signals.get("has_click_typer"):
        s["entrypoint"] += 60
    if signals.get("is_shell") and signals.get("calls_python"):
        s["run_workflow"] += 60
        s["entrypoint"] += 20
    if signals.get("mentions_train") or signals.get("defines_train_loop"):
        s["entrypoint"] += 35
        s["run_workflow"] += 25
    if signals.get("mentions_data") or signals.get("defines_dataset"):
        s["data_pipeline"] += 60
    if signals.get("mentions_model") or signals.get("defines_model"):
        s["model_definition"] += 60
    if signals.get("mentions_loss") or signals.get("defines_loss"):
        s["loss_definition"] += 70
    if signals.get("mentions_metrics") or signals.get("defines_metric") or signals.get("defines_eval_loop"):
        s["metric_evaluation"] += 60
    if signals.get("mentions_config") or signals.get("is_config_like"):
        s["config_or_arguments"] += 45
    if signals.get("is_result_like"):
        s["results_or_outputs"] += 70
    if signals.get("calls_pytest") or "/test" in p or p.startswith("test") or p.startswith("tests/"):
        s["tests"] += 70
    if signals.get("imports_torch"):
        s["model_definition"] += 15
        s["loss_definition"] += 10
        s["data_pipeline"] += 10
    return s


def _importance(role_scores: dict[str, int], signals: dict[str, Any], rel: str) -> int:
    score = max(role_scores.values()) if role_scores else 0
    score += sum(1 for v in role_scores.values() if v > 0) * 6
    if signals.get("has_main_guard"):
        score += 20
    if signals.get("is_doc"):
        score += 12
    if signals.get("is_result_like"):
        score += 12
    # Prefer concise files when scores tie.
    size = int(signals.get("size") or 0)
    if size < 30000:
        score += 6
    if size > 250000:
        score -= 20
    return int(score)


def build_repository_map(workspace: str, max_files: int = 20000, max_inspect_chars: int = 120000) -> dict[str, Any]:
    root = Path(workspace).resolve()
    files = _walk(root, max_files=max_files)
    records: list[dict[str, Any]] = []
    candidates_by_role: dict[str, list[dict[str, Any]]] = {r: [] for r in ROLES}
    project_type_signals = {
        "python": 0,
        "ml_training": 0,
        "data_processing": 0,
        "cli_tool": 0,
        "web_backend": 0,
        "research_experiment": 0,
    }
    for rel in files:
        p = root / rel
        suffix = p.suffix.lower()
        try:
            size = p.stat().st_size
        except Exception:
            size = 0
        if suffix in BINARY_SUFFIXES:
            record = {"path": rel, "size": size, "suffix": suffix, "binary_or_skipped": True, "signals": {}, "roles": {}, "importance_score": 0}
            records.append(record)
            continue
        text = _safe_read(p, max_inspect_chars) if suffix in TEXT_SUFFIXES or suffix == "" else ""
        signals = _base_signals(rel, text, size)
        symbols: dict[str, Any] = {}
        if suffix == ".py":
            symbols = _python_ast_summary(text)
            _add_python_signals(signals, symbols, text)
        roles = _role_scores(rel, signals)
        score = _importance(roles, signals, rel)
        record = {
            "path": rel,
            "size": size,
            "suffix": suffix,
            "signals": {k: v for k, v in signals.items() if isinstance(v, bool) and v or k in {"suffix", "size"}},
            "symbols": symbols,
            "roles": {k: v for k, v in roles.items() if v > 0},
            "importance_score": score,
        }
        records.append(record)
        for role, role_score in roles.items():
            if role_score > 0:
                candidates_by_role[role].append({"path": rel, "score": role_score, "importance_score": score, "signals": record["signals"], "symbols": symbols})
        if suffix == ".py":
            project_type_signals["python"] += 1
        if signals.get("imports_torch") or signals.get("defines_model") or signals.get("defines_dataset"):
            project_type_signals["ml_training"] += 1
        if signals.get("mentions_data") and not signals.get("imports_torch"):
            project_type_signals["data_processing"] += 1
        if signals.get("has_argparse") or signals.get("has_click_typer"):
            project_type_signals["cli_tool"] += 1
        if any(x in text.lower() for x in ["fastapi", "flask", "django", "uvicorn"]):
            project_type_signals["web_backend"] += 1
        if signals.get("is_result_like") or "experiment" in rel.lower() or signals.get("mentions_metrics"):
            project_type_signals["research_experiment"] += 1
    for role in ROLES:
        candidates_by_role[role] = sorted(candidates_by_role[role], key=lambda x: (x["score"], x["importance_score"]), reverse=True)[:30]
    project_types = [k for k, v in sorted(project_type_signals.items(), key=lambda kv: kv[1], reverse=True) if v > 0]
    records_sorted = sorted(records, key=lambda r: r.get("importance_score", 0), reverse=True)
    return {
        "files": files,
        "py_files": [f for f in files if f.endswith(".py")],
        "records": records,
        "top_records": records_sorted[:150],
        "candidates_by_role": candidates_by_role,
        "project_type_signals": project_type_signals,
        "project_types": project_types[:4],
        "roles": ROLES,
        "has_tests": any(f.startswith("tests/") or "/test" in f or Path(f).name.startswith("test_") for f in files),
        "has_git": (root / ".git").exists(),
    }


def compact_repo_map_for_llm(repo_map: dict[str, Any], max_records: int = 120) -> dict[str, Any]:
    def small_record(r: dict[str, Any]) -> dict[str, Any]:
        sy = r.get("symbols") or {}
        return {
            "path": r.get("path"),
            "size": r.get("size"),
            "roles": r.get("roles", {}),
            "score": r.get("importance_score", 0),
            "signals": r.get("signals", {}),
            "classes": sy.get("classes", [])[:12],
            "functions": sy.get("functions", [])[:18],
            "imports": sy.get("imports", [])[:16],
        }
    candidates = {}
    for role, items in (repo_map.get("candidates_by_role") or {}).items():
        candidates[role] = [{"path": x.get("path"), "score": x.get("score"), "signals": x.get("signals", {})} for x in items[:12]]
    return {
        "file_count": len(repo_map.get("files", [])),
        "py_file_count": len(repo_map.get("py_files", [])),
        "project_types": repo_map.get("project_types", []),
        "project_type_signals": repo_map.get("project_type_signals", {}),
        "roles": repo_map.get("roles", ROLES),
        "top_records": [small_record(r) for r in repo_map.get("top_records", [])[:max_records]],
        "candidates_by_role": candidates,
    }


def heuristic_select_evidence(repo_map: dict[str, Any], max_files: int = 32, per_role: int = 3) -> dict[str, Any]:
    selected: list[str] = []
    role_assignments: dict[str, list[str]] = {}
    for role in ROLES:
        paths = []
        for item in (repo_map.get("candidates_by_role") or {}).get(role, [])[:per_role]:
            path = item.get("path")
            if path and path not in selected:
                selected.append(path)
            if path:
                paths.append(path)
        role_assignments[role] = paths
    # Add globally important records until max_files.
    for r in repo_map.get("top_records", []):
        path = r.get("path")
        if path and path not in selected:
            selected.append(path)
        if len(selected) >= max_files:
            break
    return {
        "selected_files": selected[:max_files],
        "role_assignments": {k: v for k, v in role_assignments.items() if v},
        "missing_roles": [r for r, v in role_assignments.items() if not v],
        "reason": "heuristic selection from repository map role candidates",
    }


def coverage_check(selection: dict[str, Any], repo_map: dict[str, Any]) -> dict[str, Any]:
    selected = set(selection.get("selected_files") or [])
    role_assignments = {k: list(v) for k, v in (selection.get("role_assignments") or {}).items()}
    # Fill from selected files if LLM omitted role_assignments.
    rec_by_path = {r.get("path"): r for r in repo_map.get("records", [])}
    for path in selected:
        roles = (rec_by_path.get(path) or {}).get("roles") or {}
        for role in roles:
            role_assignments.setdefault(role, [])
            if path not in role_assignments[role]:
                role_assignments[role].append(path)
    missing = [role for role in ROLES if not role_assignments.get(role)]
    covered = [role for role in ROLES if role_assignments.get(role)]
    return {
        "ok": len(missing) == 0,
        "covered_roles": covered,
        "missing_roles": missing,
        "coverage_ratio": round(len(covered) / len(ROLES), 3),
        "role_assignments": role_assignments,
    }


def add_targeted_retrieval(selection: dict[str, Any], repo_map: dict[str, Any], max_files: int = 36) -> dict[str, Any]:
    selected = list(dict.fromkeys(selection.get("selected_files") or []))
    coverage = coverage_check(selection, repo_map)
    role_assignments = coverage["role_assignments"]
    for role in coverage["missing_roles"]:
        candidates = (repo_map.get("candidates_by_role") or {}).get(role, [])
        role_paths = []
        for item in candidates[:3]:
            path = item.get("path")
            if not path:
                continue
            if path not in selected and len(selected) < max_files:
                selected.append(path)
            role_paths.append(path)
        if role_paths:
            role_assignments[role] = role_paths
    selection = dict(selection)
    selection["selected_files"] = selected[:max_files]
    selection["role_assignments"] = role_assignments
    selection["coverage_after_targeted_retrieve"] = coverage_check(selection, repo_map)
    return selection


def role_summaries_from_evidence(selection: dict[str, Any], read_result: dict[str, Any], repo_map: dict[str, Any], max_chars_per_role: int = 3500) -> dict[str, str]:
    files = {item.get("path"): item for item in (read_result.get("data") or {}).get("files", []) if item.get("ok")}
    role_assignments = selection.get("role_assignments") or {}
    rec_by_path = {r.get("path"): r for r in repo_map.get("records", [])}
    summaries: dict[str, str] = {}
    for role in ROLES:
        chunks = []
        for path in role_assignments.get(role, [])[:5]:
            item = files.get(path)
            rec = rec_by_path.get(path) or {}
            if not item:
                continue
            symbols = rec.get("symbols") or {}
            header = f"### {path}\nroles={rec.get('roles', {})}\nclasses={symbols.get('classes', [])[:12]}\nfunctions={symbols.get('functions', [])[:16]}\n"
            content = str(item.get("content", ""))
            chunks.append(header + truncate(content, 1400))
        summaries[role] = truncate("\n\n".join(chunks), max_chars_per_role)
    return summaries
