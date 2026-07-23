from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coding_agent.core.utils import truncate


def _contains_any(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return any(t.lower() in low for t in terms)


def _dedupe(items: list[str], limit: int | None = None) -> list[str]:
    out: list[str] = []
    for x in items:
        if not isinstance(x, str):
            continue
        x = x.strip()
        if x and x not in out:
            out.append(x)
        if limit is not None and len(out) >= limit:
            break
    return out


def _script_design_requested(task: str) -> bool:
    """Return True only when the task asks for an analysis/script design.

    A plain word such as "design"/"设计" is too broad for repository reports:
    it can refer to loss design, model design, UI design, or system design.
    The analysis contract should require proposed_script_design only when the
    user specifically asks for a script/tool/CLI/report generator design.
    """
    text = task or ""
    low = text.lower()
    explicit_phrases = [
        "analysis script",
        "summary script",
        "script design",
        "design a script",
        "design the script",
        "follow-up script",
        "follow-up analysis script",
        "proposed script",
        "分析脚本",
        "脚本设计",
        "后续脚本",
        "后续分析脚本",
    ]
    if _contains_any(text, explicit_phrases):
        return True
    patterns = [
        r"(?:设计|新增|创建|生成|实现|编写).{0,30}(?:脚本|工具|cli|命令行)",
        r"(?:脚本|工具|cli|命令行).{0,30}(?:设计|方案|实现)",
        r"\bdesign\b.{0,40}\b(?:script|tool|cli|command[- ]line)\b",
        r"\b(?:script|tool|cli|command[- ]line)\b.{0,40}\b(?:design|plan|proposal)\b",
    ]
    return any(re.search(pattern, low, re.I) for pattern in patterns)


SECTION_ALIASES = {
    "Overall Purpose": ["\u9879\u76ee\u76ee\u6807", "\u603b\u4f53\u76ee\u6807", "\u9879\u76ee\u6982\u8ff0"],
    "Project Type": ["\u9879\u76ee\u7c7b\u578b", "\u9879\u76ee\u5b9a\u4f4d"],
    "Project Type and Evidence Coverage": ["\u9879\u76ee\u7c7b\u578b", "\u8bc1\u636e\u8986\u76d6"],
    "Directory Structure": ["\u76ee\u5f55\u7ed3\u6784", "\u4e3b\u8981\u76ee\u5f55"],
    "Main Entry Points": ["\u4e3b\u8981\u5165\u53e3", "\u8bad\u7ec3/\u8bc4\u4f30\u5165\u53e3", "\u5165\u53e3\u6587\u4ef6"],
    "Data Flow": ["\u6570\u636e\u6d41", "\u8f93\u5165\u8f93\u51fa"],
    "Data Flow and Inputs/Outputs": ["\u6570\u636e\u6d41", "\u8f93\u5165\u8f93\u51fa"],
    "Model": ["\u6a21\u578b", "\u6a21\u578b\u4ee3\u7801"],
    "Model / Core Logic": ["\u6a21\u578b", "\u6838\u5fc3\u903b\u8f91", "\u6a21\u578b\u4ee3\u7801"],
    "Losses": ["\u635f\u5931", "\u6307\u6807", "\u8bc4\u4f30"],
    "Losses / Metrics / Evaluation": ["\u635f\u5931", "\u6307\u6807", "\u8bc4\u4f30"],
    "Scripts": ["\u811a\u672c", "\u5de5\u4f5c\u6d41", "\u53ef\u8fd0\u884c\u547d\u4ee4", "\u8fd0\u884c\u547d\u4ee4"],
    "Scripts / Workflow / Execution": ["\u811a\u672c", "\u5de5\u4f5c\u6d41", "\u53ef\u8fd0\u884c\u547d\u4ee4", "\u8fd0\u884c\u547d\u4ee4"],
    "Configs": ["\u914d\u7f6e", "\u7ed3\u679c", "\u4ea7\u7269", "\u5b9e\u9a8c\u7ed3\u679c\u6587\u4ef6"],
    "Configs, Results, and Artifacts": ["\u914d\u7f6e", "\u7ed3\u679c", "\u4ea7\u7269", "\u5b9e\u9a8c\u7ed3\u679c\u6587\u4ef6"],
    "Risks": ["\u98ce\u9669", "\u6f5c\u5728\u98ce\u9669", "\u4e0b\u4e00\u6b65"],
    "Risks, Gaps, and Next Checks": ["\u98ce\u9669", "\u6f5c\u5728\u98ce\u9669", "\u4e0b\u4e00\u6b65"],
    "File Responsibility": ["\u6587\u4ef6\u804c\u8d23", "\u6587\u4ef6\u8d23\u4efb"],
    "File Responsibility Table": ["\u6587\u4ef6\u804c\u8d23", "\u6587\u4ef6\u8d23\u4efb"],
    "Relevant Files": ["\u76f8\u5173\u6587\u4ef6", "\u5173\u952e\u6587\u4ef6"],
    "Metric Schema": ["\u6307\u6807\u7ed3\u6784", "\u6307\u6807\u5b57\u6bb5", "\u5173\u952e\u6307\u6807"],
    "Result/Summary Schema": ["\u7ed3\u679c\u7ed3\u6784", "\u6458\u8981\u7ed3\u6784", "summary \u7ed3\u6784"],
    "Existing Aggregation Flow": ["\u805a\u5408\u6d41\u7a0b", "\u7ed3\u679c\u6536\u96c6", "\u6c47\u603b\u6d41\u7a0b"],
    "Model/Run Comparison Method": ["\u6a21\u578b\u6bd4\u8f83", "\u5b9e\u9a8c\u6bd4\u8f83", "\u8fd0\u884c\u6bd4\u8f83"],
    "Proposed Summary Analysis Script Design": ["\u5206\u6790\u811a\u672c\u8bbe\u8ba1", "\u811a\u672c\u8bbe\u8ba1"],
    "Risks and Missing Evidence": ["\u98ce\u9669", "\u7f3a\u5931\u8bc1\u636e", "\u4e0d\u786e\u5b9a"],
    "Question Coverage Checklist": ["\u95ee\u9898\u8986\u76d6", "\u68c0\u67e5\u6e05\u5355", "\u8986\u76d6\u68c0\u67e5"],
}


def _section_present(report: str, section: str) -> bool:
    lower = report.lower()
    if section.lower() in lower:
        return True
    for alias in SECTION_ALIASES.get(section, []):
        if alias and alias.lower() in lower:
            return True
    return False


def extract_report_headings(report: str, limit: int = 80) -> list[str]:
    headings: list[str] = []
    for match in re.finditer(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", report or "", re.M):
        text = re.sub(r"\s+", " ", match.group(1)).strip().strip("#").strip()
        if text:
            headings.append(text)
        if len(headings) >= limit:
            break
    return headings


def apply_section_coverage_review(check: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
    """Apply an LLM semantic heading review without weakening other gates.

    The deterministic contract checker owns evidence, path, field and question
    checks. This function only lets an external semantic reviewer say that an
    existing report heading/section covers a required section with a different
    wording.
    """
    if not review:
        return check
    try:
        out = json.loads(json.dumps(check, ensure_ascii=False))
    except Exception:
        out = dict(check)

    items = review.get("section_coverage") or review.get("sections") or []
    if not isinstance(items, list):
        return out

    covered: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("covered") is not True:
            continue
        section = str(item.get("required_section") or item.get("section") or "").strip()
        if not section:
            continue
        covered[section] = {
            "matched_heading": item.get("matched_heading"),
            "reason": item.get("reason"),
        }
    if not covered:
        out["section_coverage_review"] = review
        return out

    for section_result in out.get("required_section_results") or []:
        section = str(section_result.get("section") or "")
        if section not in covered:
            continue
        section_result["present"] = True
        section_result["semantic_present"] = True
        section_result["matched_heading"] = covered[section].get("matched_heading")
        section_result["semantic_reason"] = covered[section].get("reason")

    missing = [str(s) for s in out.get("missing_sections") or [] if str(s) not in covered]
    out["missing_sections"] = missing
    failures = [
        f
        for f in out.get("failures", [])
        if not str(f).startswith("missing required task-focused sections:")
    ]
    if missing:
        failures.append(f"missing required task-focused sections: {missing}")
    out["failures"] = failures
    out["ok"] = not failures
    out["section_coverage_review"] = review
    return out


def collect_analysis_evidence_paths(analysis_context: dict[str, Any]) -> list[str]:
    """Collect concrete repository paths that can count as cited evidence.

    Selected snippets are only one source of evidence. Focused reports often cite
    role-summary paths, structured memory paths, or files read during schema
    probing. Counting only selected_files makes the quality gate brittle when the
    selector picks many near-duplicate result files.
    """
    paths: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            item = value.strip().strip("`'\"")
            if not item:
                return
            has_separator = "/" in item or "\\" in item
            has_known_suffix = bool(re.search(r"\.(?:py|sh|json|jsonl|yaml|yml|toml|ini|cfg|md|txt|csv|tsv|npz|pt|pth)\b", item, re.I))
            if has_separator or has_known_suffix:
                paths.append(item.replace("\\", "/"))
        elif isinstance(value, dict):
            for key in value:
                add(key)
            for key in ("path", "file", "target_file"):
                if key in value:
                    add(value.get(key))
            for nested in value.values():
                if isinstance(nested, (list, tuple, dict)):
                    add(nested)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)

    add(analysis_context.get("selected_files") or [])
    add(analysis_context.get("structured_memory") or {})
    add(analysis_context.get("role_summaries") or {})
    add((analysis_context.get("compact_repo_map") or {}).get("candidates_by_role") or {})
    add((analysis_context.get("read_result") or {}).get("data") or {})
    add(analysis_context.get("repo_map", {}).get("files") or [])
    return _dedupe(paths, limit=500)


def analysis_path_mentions(report: str, analysis_context: dict[str, Any]) -> list[str]:
    return [p for p in collect_analysis_evidence_paths(analysis_context) if p and p in report]


def build_analysis_contract(task: str, task_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a task-focused analysis contract.

    This is intentionally generic: it does not hard-code a project. It turns the
    user's analysis request into required questions and focus terms so the report
    can be checked against the actual ask rather than a fixed repository-overview
    template.
    """
    task = task or ""
    low = task.lower()
    questions: list[dict[str, Any]] = []
    focus_keywords: list[str] = []
    report_type = "repository_overview"
    broad_overview_requested = _contains_any(
        task,
        [
            "项目理解",
            "理解这个项目",
            "项目目标",
            "主要目录",
            "目录结构",
            "项目结构",
            "project understanding",
            "project overview",
            "repository overview",
            "codebase overview",
        ],
    )

    def add(qid: str, question: str, terms: list[str], required: bool = True) -> None:
        if not any(q["id"] == qid for q in questions):
            questions.append({"id": qid, "question": question, "required_terms": terms, "required": required})
        focus_keywords.extend(terms)

    if broad_overview_requested:
        add("project_overview", "Explain the repository purpose, directory structure, main entrypoints, workflows, and risks.", ["project", "overview", "directory", "entry", "script", "workflow", "risk", "项目", "目录", "入口", "风险"])

    # Task-family detection; generic keywords, bilingual where relevant.
    if _contains_any(task, ["metric", "metrics", "指标", "iou", "mae", "accuracy", "准确", "summary", "result", "结果", "collect_results", "比较", "ranking", "排名"]):
        if not broad_overview_requested:
            report_type = "metric_result_summary"
        add("metrics_files", "Identify the metrics/evaluation files and the concrete metric names they define or compute.", ["metric", "metrics", "accuracy", "acc", "iou", "mae", "loss", "score", "指标"])
        add("result_summary_files", "Identify summary/result/artifact files and the schema or fields that can be read from them.", ["summary", "result", "artifact", "json", "结果"])
        add("aggregation_flow", "Explain any existing aggregation/collection flow, including files or scripts that collect results.", ["collect", "aggregate", "summary", "result", "script", "workflow"])
        add("comparison_method", "Explain how to compare different models/runs, including metric direction and ranking/table logic.", ["compare", "comparison", "rank", "ranking", "higher", "lower", "best", "table", "比较", "排名"])
    if _script_design_requested(task) or _contains_any(task, ["输出哪些表格"]):
        if not broad_overview_requested:
            report_type = "metric_result_summary" if report_type != "repository_overview" else "script_design_analysis"
        add("proposed_script_design", "Design the follow-up analysis script: input files, parsed fields, output columns/tables, sorting/ranking logic, and usage command.", ["script", "input", "output", "columns", "table", "usage", "command", "sort", "rank", "设计", "表格"])
    if _contains_any(task, ["训练入口", "entry", "入口", "运行脚本", "script", "workflow", "训练", "run"]):
        add("entrypoints_workflow", "List training/runtime entrypoints and workflow scripts with concrete paths and expected roles.", ["train", "entry", "main", "script", "workflow", "run", "torchrun", "训练", "入口"])
    if _contains_any(task, ["数据输入", "input", "inputs", "字段", "key", "keys", "dataset", "data", "数据"]):
        add("data_inputs", "Describe data input files, datasets, and field/key names when evidence is available.", ["data", "dataset", "input", "key", "field", "npz", "json", "csv", "数据", "字段"])
    # Add a model-structure question only when the user asks about model files/classes/architecture.
    # Phrases like "compare different models" belong to comparison_method, not model_definition.
    if _contains_any(task, ["模型结构", "模型文件", "模型类", "model class", "model file", "architecture", "网络结构", "class", "classes"]):
        add("models", "Identify model/core logic files and principal classes/functions.", ["model", "module", "class", "network", "transformer", "cnn", "resnet", "mlp", "模型"])
    if _contains_any(task, ["loss", "损失"]):
        add("losses", "Identify loss files and concrete loss names/functions.", ["loss", "criterion", "bce", "focal", "cross", "损失"])
    if _contains_any(task, ["配置", "config", "实验矩阵", "matrix", "kernel", "实验"]):
        add("configs_experiment_matrix", "Identify configs, experiment axes, and script/config matrices.", ["config", "yaml", "json", "argument", "kernel", "experiment", "matrix", "配置", "实验"])

    if not questions:
        add(
            "overview",
            "Explain the repository purpose, structure, entrypoints, data flow, models/core logic, tests, and risks.",
            ["overview", "entry", "data", "model", "test", "risk", "项目", "目录", "入口", "数据", "模型", "测试", "风险"],
        )

    focus_keywords = _dedupe(focus_keywords, limit=80)
    required_sections = _sections_for_report_type(report_type, questions)
    return {
        "version": "v1.13",
        "report_type": report_type,
        "task": task,
        "objective": (task_spec or {}).get("objective", task),
        "required_questions": questions,
        "focus_keywords": focus_keywords,
        "required_sections": required_sections,
        "quality_gates": [
            "answers_all_required_questions",
            "mentions_concrete_file_paths",
            "avoids_ungrounded_generic_claims",
            "uses_task_focused_report_shape",
        ],
    }


def _sections_for_report_type(report_type: str, questions: list[dict[str, Any]]) -> list[str]:
    if report_type in {"metric_result_summary", "script_design_analysis"}:
        return [
            "Relevant Files",
            "Metric Schema",
            "Result/Summary Schema",
            "Existing Aggregation Flow",
            "Model/Run Comparison Method",
            "Proposed Summary Analysis Script Design",
            "Risks and Missing Evidence",
            "Question Coverage Checklist",
        ]
    return [
        "Overall Purpose",
        "Project Type and Evidence Coverage",
        "Directory Structure",
        "Main Entry Points",
        "Data Flow and Inputs/Outputs",
        "Model / Core Logic",
        "Losses / Metrics / Evaluation",
        "Scripts / Workflow / Execution",
        "Configs, Results, and Artifacts",
        "Risks, Gaps, and Next Checks",
        "File Responsibility Table",
        "Question Coverage Checklist",
    ]


def build_task_focused_file_hints(contract: dict[str, Any], repo_map: dict[str, Any], profile: dict[str, Any] | None = None, max_files: int = 18) -> list[str]:
    valid = set(repo_map.get("files") or [])
    keywords = [k.lower() for k in (contract.get("focus_keywords") or [])]
    out: list[str] = []

    # Structured memory is preferred if present because it is the distilled result of prior runs.
    structured = (profile or {}).get("structured_memory") or {}
    for bucket in ["metric_files", "summary_files", "result_files", "collector_files", "analysis_script_inputs", "script_files"]:
        for p in structured.get(bucket, []) or []:
            if p in valid and p not in out:
                out.append(p)
            if len(out) >= max_files:
                return out

    # Then use path/signal matches from the full repository map.
    for rec in sorted(repo_map.get("records", []), key=lambda r: r.get("importance_score", 0), reverse=True):
        p = rec.get("path") or ""
        low_path = p.lower()
        signals = rec.get("signals") or {}
        roles = rec.get("roles") or {}
        score_hit = False
        if any(k and k in low_path for k in keywords):
            score_hit = True
        if contract.get("report_type") == "metric_result_summary":
            if roles.get("metric_evaluation") or roles.get("results_or_outputs") or signals.get("is_result_like"):
                score_hit = True
            if any(k in low_path for k in ["collect", "summary", "metric", "result", "eval", "score"]):
                score_hit = True
        if score_hit and p in valid and p not in out:
            out.append(p)
        if len(out) >= max_files:
            break
    return out


def extract_structured_memory(state: dict[str, Any]) -> dict[str, Any]:
    repo_map = state.get("repo_map") or {}
    ctx = state.get("repo_analysis_context") or {}
    evidence_index = ctx.get("evidence_index") or state.get("evidence_index") or {}
    read_result = ctx.get("read_result") or {}
    records = repo_map.get("records", []) or []

    def path_bucket(pred) -> list[str]:
        vals = []
        for r in records:
            p = r.get("path") or ""
            if pred(p.lower(), r):
                vals.append(p)
        return _dedupe(vals, limit=60)

    metric_files = path_bucket(lambda p, r: bool((r.get("roles") or {}).get("metric_evaluation")) or any(k in p for k in ["metric", "eval", "score", "iou", "mae", "accuracy", "acc"]))
    result_files = path_bucket(lambda p, r: bool((r.get("roles") or {}).get("results_or_outputs")) or any(k in p for k in ["summary", "result", "score", "report"]))
    collector_files = path_bucket(lambda p, r: any(k in p for k in ["collect", "aggregate", "summary"]) and p.endswith((".py", ".sh", ".md")))
    script_files = path_bucket(lambda p, r: bool((r.get("roles") or {}).get("run_workflow")) or p.endswith((".sh", ".bash")))
    config_files = path_bucket(lambda p, r: bool((r.get("roles") or {}).get("config_or_arguments")) or p.endswith((".json", ".yaml", ".yml", ".toml", ".ini", ".cfg")))

    metric_names = []
    for terms in (evidence_index.get("metric_or_domain_terms_by_file") or {}).values():
        metric_names.extend([str(t) for t in terms])
    metric_names.extend(_extract_metric_terms_from_read_files(read_result))
    result_json_keys = _extract_json_keys_from_read_files(read_result)
    experiment_like_groups = _extract_experiment_groups(repo_map)

    return {
        "version": "v1.13",
        "metric_files": metric_files[:40],
        "summary_files": [p for p in result_files if "summary" in p.lower()][:40],
        "result_files": result_files[:60],
        "collector_files": collector_files[:30],
        "script_files": script_files[:40],
        "config_files": config_files[:40],
        "metric_names": _dedupe(metric_names, limit=80),
        "result_json_keys": result_json_keys[:120],
        "experiment_like_groups": experiment_like_groups[:80],
        "analysis_script_inputs": _dedupe(metric_files[:10] + result_files[:20] + collector_files[:10], limit=40),
    }


def _extract_metric_terms_from_read_files(read_result: dict[str, Any]) -> list[str]:
    files = ((read_result.get("data") or {}).get("files") or [])
    terms: list[str] = []
    pat = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*(?:accuracy|acc|iou|mae|mse|loss|score|metric|precision|recall|f1|auc)[a-zA-Z0-9_]*\b", re.I)
    for item in files:
        if not item.get("ok"):
            continue
        content = str(item.get("content", ""))
        terms.extend(pat.findall(content))
    return terms


def _extract_json_keys_from_read_files(read_result: dict[str, Any]) -> list[str]:
    files = ((read_result.get("data") or {}).get("files") or [])
    out: list[str] = []
    for item in files:
        path = str(item.get("path", ""))
        if not item.get("ok") or not path.lower().endswith(".json"):
            continue
        content = str(item.get("content", ""))
        try:
            obj = json.loads(content)
        except Exception:
            continue
        keys = []
        _collect_json_keys(obj, keys, prefix="")
        out.extend([f"{path}:{k}" for k in keys[:80]])
    return _dedupe(out, limit=160)


def _collect_json_keys(obj: Any, out: list[str], prefix: str = "") -> None:
    if len(out) > 200:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.append(key)
            _collect_json_keys(v, out, key)
    elif isinstance(obj, list) and obj:
        _collect_json_keys(obj[0], out, prefix + "[]" if prefix else "[]")


def _extract_experiment_groups(repo_map: dict[str, Any]) -> list[str]:
    groups: list[str] = []
    for p in repo_map.get("files", []) or []:
        parts = Path(p).parts
        for i, part in enumerate(parts[:-1]):
            if part.lower() in {"experiment", "experiments", "runs", "outputs", "results"} and i + 1 < len(parts):
                groups.append("/".join(parts[: i + 2]))
    return _dedupe(groups, limit=120)


def verify_analysis_report_against_contract(
    report: str,
    contract: dict[str, Any],
    analysis_context: dict[str, Any],
    section_coverage_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lower = report.lower()
    selected_files = analysis_context.get("selected_files") or []
    structured = analysis_context.get("structured_memory") or {}
    question_results: list[dict[str, Any]] = []
    missing_required: list[str] = []

    for q in contract.get("required_questions") or []:
        terms = [str(t).lower() for t in q.get("required_terms", [])]
        term_hits = [t for t in terms if t and t in lower]
        # For very broad questions, require at least one term; for focused questions require more evidence.
        min_hits = 1 if len(terms) <= 3 else 2
        answered = len(term_hits) >= min_hits
        # Script design must include implementation-level specifics, not just say "write a script".
        if q.get("id") == "proposed_script_design":
            design_terms = ["input", "output", "column", "table", "usage", "command", "sort", "rank", "script", "读取", "输出", "表格"]
            design_hits = [t for t in design_terms if t in lower]
            answered = answered and len(design_hits) >= 4
        if q.get("id") == "comparison_method":
            compare_terms = ["higher", "lower", "rank", "sort", "best", "越大", "越小", "排名", "比较"]
            answered = answered and any(t in lower for t in compare_terms)
        if q.get("required", True) and not answered:
            missing_required.append(q.get("id", "unknown"))
        question_results.append({
            "id": q.get("id"),
            "question": q.get("question"),
            "answered": answered,
            "term_hits": term_hits[:20],
            "required": q.get("required", True),
        })

    required_section_results = []
    missing_sections = []
    for section in contract.get("required_sections") or []:
        present = _section_present(report, section)
        required_section_results.append({"section": section, "present": present})
        if not present:
            missing_sections.append(section)

    # If structured memory knows concrete metric names, encourage at least a few to appear.
    metric_names = [m for m in structured.get("metric_names", []) if isinstance(m, str)]
    metric_hits = [m for m in metric_names if m in report]
    metric_warning = None
    if contract.get("report_type") == "metric_result_summary" and metric_names and len(metric_hits) < min(2, len(metric_names)):
        metric_warning = "report mentions too few concrete metric names from evidence/structured memory"

    failures: list[str] = []
    if missing_required:
        failures.append(f"missing required analysis questions: {missing_required}")
    if missing_sections:
        failures.append(f"missing required task-focused sections: {missing_sections}")
    path_mentions = analysis_path_mentions(report, analysis_context)
    evidence_paths = analysis_context.get("contract_relevant_evidence_paths") or collect_analysis_evidence_paths(analysis_context)
    if contract.get("report_type") == "repository_overview":
        min_path_mentions = max(2, min(6, len(evidence_paths or selected_files) // 4))
    else:
        evidence_count = len(evidence_paths or selected_files)
        min_path_mentions = max(2, min(5, evidence_count, len(contract.get("required_questions") or []) + 1))
    if len(path_mentions) < min_path_mentions:
        failures.append("report cites too few selected evidence paths")
    if metric_warning:
        failures.append(metric_warning)

    result = {
        "ok": not failures,
        "report_type": contract.get("report_type"),
        "question_results": question_results,
        "missing_required_questions": missing_required,
        "required_section_results": required_section_results,
        "missing_sections": missing_sections,
        "path_mentions_count": len(path_mentions),
        "path_mentions": path_mentions[:60],
        "evidence_paths_count": len(evidence_paths),
        "min_path_mentions": min_path_mentions,
        "structured_metric_names_count": len(metric_names),
        "structured_metric_hits": metric_hits[:40],
        "failures": failures,
    }
    if section_coverage_review:
        result = apply_section_coverage_review(result, section_coverage_review)
    return result
