from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from coding_agent.scope.read_only_policy import detect_global_read_only_lock, apply_read_only_lock_to_intent
from coding_agent.scope.write_scope import extract_mentioned_paths, normalize_rel, explicit_global_read_only_requested
from coding_agent.scope.write_guard import path_has_explicit_create_intent, path_has_read_reference_intent, path_has_output_intent, is_probably_code_output, is_test_path
from coding_agent.contracts.artifact_constraints import (
    detect_prohibited_artifacts,
    is_prohibited_artifact_path,
    is_test_artifact_path,
    tests_creation_prohibited,
)
from coding_agent.scope.scope_contract import build_scope_contract, path_is_protected, path_operation
from coding_agent.scope.semantic_write_scope import merge_semantic_scope_contract, resolve_semantic_write_scope
from coding_agent.workspace.run_paths import is_under_test_support_dir

CREATE_WORDS_ZH = ["新增", "创建", "新建", "生成", "写一个", "写个", "做一个", "做个", "实现", "添加", "允许创建", "创建新文件", "创建新脚本", "新增脚本", "新增一个脚本", "写一个脚本"]
CREATE_WORDS_EN = ["create", "add", "write", "generate", "implement", "new script", "new file", "allowed to create"]
FIX_WORDS_ZH = ["修复", "修一下", "修一修", "修个", "报错", "失败", "跑不通", "改错", "让 pytest 通过", "使 pytest 通过"]
FIX_WORDS_EN = ["fix", "debug", "repair", "failing", "error", "traceback", "make pytest pass"]
MODIFY_WORDS_ZH = ["修改", "重构", "改成", "加入", "增加", "加一个", "加个"]
MODIFY_WORDS_EN = ["modify", "refactor", "change", "patch"]
ANALYZE_WORDS_ZH = ["分析", "讲解", "总结", "查看", "看看", "看一下", "看下", "扫描", "读取项目结构"]
ANALYZE_WORDS_EN = ["analyze", "inspect", "explain", "summarize", "review", "scan"]
NEGATION_WORDS_ZH = ["禁止", "不要", "不得", "不能", "不许", "不允许", "无需", "无须", "只读", "只分析", "只查看"]
NEGATION_WORDS_EN = ["do not", "don't", "dont", "must not", "never", "no ", "without", "read-only", "read only", "only analyze", "only inspect"]

SCRIPT_READ_ONLY_WORDS_ZH = [
    "\u53ea\u8bfb\u5206\u6790\u811a\u672c",  # 只读分析脚本
    "\u811a\u672c\u8fd0\u884c\u65f6\u53ea\u8bfb\u53d6",  # 脚本运行时只读取
    "\u65b0\u811a\u672c\u8fd0\u884c\u65f6\u53ea\u8bfb\u53d6",  # 新脚本运行时只读取
    "\u811a\u672c\u5e94\u8bfb\u53d6",  # 脚本应读取
    "\u53ea\u8bfb\u53d6",  # 只读取
]
SCAN_FIRST_WORDS_ZH = [
    "\u5148\u53ea\u8bfb\u626b\u63cf",  # 先只读扫描
    "\u5148\u626b\u63cf",  # 先扫描
    "\u5148\u8bfb\u53d6\u9879\u76ee\u7ed3\u6784",  # 先读取项目结构
    "\u5148\u8bfb\u53d6",  # 先读取
    "\u5148\u67e5\u770b",  # 先查看
]


SYMBOLIC_AGENT_TEST_IDS = {
    "<thread-id>",
    "<thread_id>",
    "{thread-id}",
    "{thread_id}",
    "$thread-id",
    "$thread_id",
}


def is_symbolic_agent_test_path(path: Any) -> bool:
    """Return whether a path describes the runtime test location, not a file.

    User prompts and documentation use ``.coding_agent_test/<thread-id>`` as
    a policy placeholder. Treating that text as a concrete create target can
    produce a literal file named ``<thread-id>``.
    """
    rel = normalize_rel(str(path or "").strip().strip("'\""))
    parts = rel.split("/")
    return (
        len(parts) >= 2
        and parts[0] == ".coding_agent_test"
        and parts[1].lower() in SYMBOLIC_AGENT_TEST_IDS
    )


def _normalize_llm_create_path(path: Any) -> str:
    raw = str(path or "").strip().strip("'\"")
    if raw.replace("\\", "/").endswith("/"):
        return ""
    rel = normalize_rel(raw)
    if is_symbolic_agent_test_path(rel):
        return ""
    if not rel or "*" in rel or rel.endswith("/"):
        return ""
    if rel.startswith(".coding_agent/"):
        return ""
    if ".." in Path(rel).parts or rel.startswith("/"):
        return ""
    return normalize_rel(rel)


def _llm_create_path_is_concrete_file(rel: str) -> bool:
    rel = normalize_rel(rel)
    if not rel:
        return False
    name = Path(rel).name.lower()
    if name in {"dockerfile", "makefile", "procfile"}:
        return True
    return bool(Path(rel).suffix) or is_probably_code_output(rel) or is_test_path(rel)


def _llm_create_paths(llm_obj: dict[str, Any]) -> list[str]:
    raw = llm_obj.get("create_paths") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    task_type = str(llm_obj.get("task_type") or "")
    for item in raw:
        rel = _normalize_llm_create_path(item)
        if not rel:
            continue
        # For write_script tasks, only accept code/readme/test deliverables from
        # the LLM. Data/result paths remain references unless the planner later
        # explicitly requests them under a writable mode.
        if task_type == "write_script" and not (is_probably_code_output(rel) or is_test_path(rel)):
            continue
        if rel not in out:
            out.append(rel)
    return out


def _llm_requests_current_create(llm_obj: dict[str, Any]) -> bool:
    task_type = str(llm_obj.get("task_type") or "")
    return (
        task_type in {"write_script", "generate_project"}
        or bool(_llm_create_paths(llm_obj))
        or (llm_obj.get("read_only") is False and task_type not in {"analyze", "other"})
    )


def _contains_any(task: str, zh_words: list[str], en_words: list[str]) -> bool:
    low = (task or "").lower()
    return any(w in (task or "") for w in zh_words) or any(w in low for w in en_words)


def _has_unnegated_word(task: str, zh_words: list[str], en_words: list[str], *, window: int = 32) -> bool:
    """Return true only for positive intent words.

    A phrase such as "禁止创建、修改、删除任何文件" contains create/modify words,
    but it is a constraint, not a write request. This helper keeps those
    negative clauses from turning read-only analysis into write mode.
    """
    text = task or ""
    low = text.lower()
    checks: list[tuple[str, str, bool]] = []
    checks.extend((text, w, False) for w in zh_words)
    checks.extend((low, w.lower(), True) for w in en_words)
    for haystack, word, is_en in checks:
        if not word:
            continue
        start = 0
        while True:
            idx = haystack.find(word, start)
            if idx < 0:
                break
            if is_en:
                before_ch = haystack[idx - 1] if idx > 0 else ""
                after_idx = idx + len(word)
                after_ch = haystack[after_idx] if after_idx < len(haystack) else ""
                if (before_ch and (before_ch.isalnum() or before_ch in "_-")) or (
                    after_ch and (after_ch.isalnum() or after_ch in "_-")
                ):
                    start = idx + max(1, len(word))
                    continue
            before = haystack[max(0, idx - window):idx]
            before_low = before.lower()
            negated = any(n in before for n in NEGATION_WORDS_ZH) or any(n in before_low for n in NEGATION_WORDS_EN)
            if not negated:
                return True
            start = idx + max(1, len(word))
    return False


def _test_equivalence_key(path: str) -> str | None:
    rel = normalize_rel(path)
    if not is_test_artifact_path(rel):
        return None
    name = Path(rel).name
    stem = Path(name).stem
    if stem.startswith("test_"):
        stem = stem[5:]
    elif stem.endswith("_test"):
        stem = stem[:-5]
    return stem or name


def _prefer_test_create_path(path: str) -> tuple[int, int, str]:
    rel = normalize_rel(path)
    in_tests_dir = is_under_test_support_dir(rel)
    canonical_tests_dir = rel.startswith("tests/")
    return (2 if canonical_tests_dir else (1 if in_tests_dir else 0), -len(rel), rel)


def _canonical_test_create_path(path: str) -> str:
    rel = normalize_rel(path)
    if not is_test_artifact_path(rel):
        return rel
    if "/" not in rel and Path(rel).suffix == ".py":
        return normalize_rel(f"tests/{Path(rel).name}")
    return rel


def _dedupe_equivalent_test_create_paths(
    create_paths: list[str],
    path_mentions: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    best_by_key: dict[str, str] = {}
    non_tests: list[str] = []
    for path in create_paths:
        rel = _canonical_test_create_path(path)
        key = _test_equivalence_key(rel)
        if not key:
            if rel not in non_tests:
                non_tests.append(rel)
            continue
        current = best_by_key.get(key)
        if current is None or _prefer_test_create_path(rel) > _prefer_test_create_path(current):
            best_by_key[key] = rel
    deduped = non_tests + sorted(best_by_key.values())
    allowed_create_paths = set(deduped)
    deduped_mentions: list[dict[str, Any]] = []
    seen_mentions: set[tuple[str, str]] = set()
    for mention in path_mentions:
        rel = _canonical_test_create_path(str(mention.get("path") or ""))
        if mention.get("intent") == "create_target" and rel not in allowed_create_paths:
            continue
        if rel != normalize_rel(str(mention.get("path") or "")):
            mention = dict(mention)
            mention["path"] = rel
        key = (rel, str(mention.get("intent") or ""))
        if key in seen_mentions:
            continue
        seen_mentions.add(key)
        deduped_mentions.append(mention)
    return deduped, deduped_mentions


def _local_context(task: str, path: str, window: int = 90) -> str:
    if not task or not path or path not in task:
        return ""
    i = task.find(path)
    return task[max(0, i-window): i + len(path) + window]


def classify_task_intent(task: str, llm_obj: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve task intent with explicit separation of global agent permission,
    script runtime read-only behavior, and initial scan behavior.

    The LLM may provide a first-pass interpretation, but this resolver enforces
    internal consistency: a prompt that asks to create/add/write files is not a
    global read-only task merely because it says the new script should be read-only.
    """
    llm_obj = llm_obj or {}
    task = task or ""
    read_only_policy = detect_global_read_only_lock(task)
    prohibited_artifacts = detect_prohibited_artifacts(task)
    prohibit_tests = tests_creation_prohibited(task)
    mentioned = [
        normalize_rel(p)
        for p in extract_mentioned_paths(task)
        if not is_symbolic_agent_test_path(p)
    ]
    scope_contract = build_scope_contract(task, mentioned)
    semantic_write_scope = resolve_semantic_write_scope(task, llm_obj, mentioned)
    semantic_controls_mode = bool(semantic_write_scope.get("available") and semantic_write_scope.get("valid"))
    semantic_operation_mode = str(semantic_write_scope.get("operation_mode") or "")
    scope_contract = merge_semantic_scope_contract(scope_contract, semantic_write_scope)
    path_mentions: list[dict[str, Any]] = []
    create_paths: list[str] = []
    read_refs: list[str] = []
    ambiguous_paths: list[str] = []
    allowed_modify_paths: list[str] = []
    protected_existing_paths: list[str] = []

    for p in mentioned:
        scope_op = path_operation(scope_contract, p)
        explicit_create = path_has_explicit_create_intent(task, p)
        read_ref = path_has_read_reference_intent(task, p)
        code_like = is_probably_code_output(p) or is_test_path(p)
        prohibited_artifact = is_prohibited_artifact_path(p, prohibited_artifacts)
        protected_by_scope = path_is_protected(scope_contract, p, include_globs=False)
        if protected_by_scope or scope_op == "forbid_modify":
            intent = "protected_reference"
            if p not in protected_existing_paths:
                protected_existing_paths.append(p)
        elif scope_op == "allow_modify":
            intent = "modify_target"
            if p not in allowed_modify_paths:
                allowed_modify_paths.append(p)
        elif (not prohibited_artifact) and (
            explicit_create
            or (
                code_like
                and _has_unnegated_word(_local_context(task, p), CREATE_WORDS_ZH, CREATE_WORDS_EN)
                and not read_ref
            )
        ):
            intent = "create_target"
            if p not in create_paths:
                create_paths.append(p)
        elif prohibited_artifact:
            intent = "prohibited_artifact"
        elif read_ref:
            intent = "read_reference"
            if p not in read_refs:
                read_refs.append(p)
        else:
            intent = "ambiguous_reference"
            ambiguous_paths.append(p)
        path_mentions.append({
            "path": p,
            "intent": intent,
            "explicit_create": explicit_create,
            "read_reference": read_ref,
            "scope_operation": scope_op,
            "context": _local_context(task, p),
            "prohibited_artifact": prohibited_artifact,
            "protected_existing": protected_by_scope,
        })

    for p in semantic_write_scope.get("protected_existing_paths") or []:
        if p not in protected_existing_paths:
            protected_existing_paths.append(p)
            path_mentions.append({
                "path": p,
                "intent": "protected_reference",
                "explicit_create": False,
                "read_reference": False,
                "context": "LLM semantic write scope protected this path",
                "prohibited_artifact": False,
                "scope_operation": "forbid_modify",
                "protected_existing": True,
            })

    for p in semantic_write_scope.get("allowed_modify_paths") or []:
        if p not in allowed_modify_paths and p not in protected_existing_paths:
            allowed_modify_paths.append(p)
            path_mentions.append({
                "path": p,
                "intent": "modify_target",
                "explicit_create": False,
                "read_reference": False,
                "context": "LLM semantic write scope allowed modifying this path",
                "prohibited_artifact": False,
                "scope_operation": "allow_modify",
                "protected_existing": False,
            })

    for p in semantic_write_scope.get("read_reference_paths") or []:
        if path_has_output_intent(task, p):
            if p not in create_paths and p not in protected_existing_paths:
                create_paths.append(p)
                path_mentions.append({
                    "path": p,
                    "intent": "create_target",
                    "explicit_create": True,
                    "read_reference": False,
                    "context": "local path wording treats this LLM read reference as an output artifact",
                    "prohibited_artifact": False,
                    "scope_operation": "mention",
                    "protected_existing": False,
                })
            continue
        if p not in read_refs and p not in protected_existing_paths:
            read_refs.append(p)
            path_mentions.append({
                "path": p,
                "intent": "read_reference",
                "explicit_create": False,
                "read_reference": True,
                "context": "LLM semantic write scope marked this as a read reference",
                "prohibited_artifact": False,
                "scope_operation": "read_reference",
                "protected_existing": False,
            })

    semantic_create_candidates = []
    mentioned_set = set(mentioned)
    for p in semantic_write_scope.get("create_paths") or []:
        if is_symbolic_agent_test_path(p):
            continue
        # In source-modify/debug tasks the LLM often has not scanned the repo
        # when it suggests auxiliary test paths. Treat those as planning hints,
        # not hard user-required output paths, unless the user explicitly named
        # the path in the task. This prevents guessed paths like test_foo.py
        # from becoming final-gate artifact requirements.
        if semantic_operation_mode == "scoped_modify" and p not in mentioned_set:
            continue
        if p not in mentioned_set and not _llm_create_path_is_concrete_file(p):
            continue
        semantic_create_candidates.append(p)
    accept_llm_create_paths = (not semantic_controls_mode) or semantic_operation_mode == "safe_create"
    if accept_llm_create_paths:
        semantic_create_candidates.extend(_llm_create_paths(llm_obj))

    for p in semantic_create_candidates:
        if is_symbolic_agent_test_path(p):
            continue
        if path_is_protected(scope_contract, p, include_globs=False):
            if p not in protected_existing_paths:
                protected_existing_paths.append(p)
            path_mentions.append({
                "path": p,
                "intent": "protected_reference",
                "explicit_create": False,
                "read_reference": False,
                "context": "LLM intake create_path rejected by protected path constraint",
                "prohibited_artifact": False,
                "scope_operation": "forbid_modify",
                "protected_existing": True,
            })
            continue
        if is_prohibited_artifact_path(p, prohibited_artifacts):
            path_mentions.append({
                "path": p,
                "intent": "prohibited_artifact",
                "explicit_create": False,
                "read_reference": False,
                "context": "LLM intake create_path rejected by prohibited artifact constraint",
                "prohibited_artifact": True,
                "scope_operation": "mention",
                "protected_existing": False,
            })
            continue
        if p in read_refs:
            read_refs.remove(p)
        if p not in create_paths:
            create_paths.append(p)
            path_mentions.append({
                "path": p,
                "intent": "create_target",
                "explicit_create": True,
                "read_reference": False,
                "context": "LLM intake create_paths normalized by task intent resolver",
                "prohibited_artifact": False,
                "scope_operation": "mention",
                "protected_existing": False,
            })

    if semantic_controls_mode:
        if semantic_operation_mode != "scoped_modify":
            allowed_modify_paths = []
        if semantic_operation_mode == "read_only_analysis":
            create_paths = []
        elif semantic_operation_mode == "scoped_modify":
            semantic_create_set = set(semantic_write_scope.get("create_paths") or [])
            create_paths = [p for p in create_paths if p in semantic_create_set]

    positive_create_requested = _has_unnegated_word(task, CREATE_WORDS_ZH, CREATE_WORDS_EN)
    llm_task_type = str(llm_obj.get("task_type") or "")
    llm_current_create_requested = (
        llm_task_type in {"write", "write_script", "generate_project", "create", "implement"}
        or bool(create_paths)
    )
    if semantic_controls_mode:
        semantic_task_mode = str(semantic_write_scope.get("task_mode") or "")
        create_requested = bool(create_paths) or semantic_operation_mode == "safe_create"
        fix_requested = semantic_operation_mode == "scoped_modify" and semantic_task_mode == "debug"
        modify_requested = semantic_operation_mode == "scoped_modify"
        analyze_requested = semantic_operation_mode == "read_only_analysis"
    else:
        create_requested = bool(create_paths) or positive_create_requested or llm_current_create_requested
        fix_requested = _contains_any(task, FIX_WORDS_ZH, FIX_WORDS_EN)
        raw_modify_requested = _has_unnegated_word(task, MODIFY_WORDS_ZH, MODIFY_WORDS_EN)
        # Negative constraints such as "不修改源码" protect scope; they do not request modification.
        negative_modify_only = any(x in task for x in ["不修改", "不要修改", "禁止修改", "不改", "不要改", "不能改"]) and not any(x in task for x in ["修改为", "改成", "修复", "新增", "创建", "写一个", "生成", "实现"])
        modify_requested = bool((raw_modify_requested and not negative_modify_only) or allowed_modify_paths)
        analyze_requested = _contains_any(task, ANALYZE_WORDS_ZH, ANALYZE_WORDS_EN)

    create_paths, path_mentions = _dedupe_equivalent_test_create_paths(create_paths, path_mentions)

    # These are separate notions; do not collapse them into one read_only flag.
    script_read_only = any(x in task for x in ["只读分析脚本", "脚本运行时只读取", "新脚本运行时只读取", "脚本应读取", "只读取"]) or any(
        x in task.lower() for x in ["read-only script", "script should read", "script reads", "only read"]
    )
    scan_first = any(x in task for x in ["先只读扫描", "先扫描", "先读取项目结构", "创建脚本前可以读取", "先查看项目结构"]) or any(
        x in task.lower()
        for x in [
            "scan first",
            "inspect first",
            "read project first",
            "read the project first",
            "read repository first",
            "read the repository first",
            "read project structure first",
            "read the project structure first",
        ]
    )
    if semantic_controls_mode:
        agent_read_only = (
            semantic_operation_mode == "read_only_analysis"
            and not create_requested
            and not fix_requested
            and not modify_requested
        )
    else:
        agent_read_only = (
            (explicit_global_read_only_requested(task) or bool(llm_obj.get("read_only") is True and not create_paths and not positive_create_requested))
            and not create_requested
            and not fix_requested
            and not modify_requested
        )

    if semantic_controls_mode:
        mode = str(semantic_write_scope.get("mode") or "analyze")
        operation_mode = semantic_operation_mode or "read_only_analysis"
        if agent_read_only:
            mode = "analyze"
            operation_mode = "read_only_analysis"
    elif agent_read_only:
        mode = "analyze"
        operation_mode = "read_only_analysis"
    elif fix_requested:
        mode = "debug"
        operation_mode = "scoped_modify"
    elif modify_requested:
        mode = "modify"
        operation_mode = "scoped_modify"
    elif create_requested:
        mode = "write"
        operation_mode = "safe_create"
    elif analyze_requested or llm_obj.get("task_type") == "analyze":
        mode = "analyze"
        operation_mode = "read_only_analysis"
    else:
        mode = "write" if (llm_obj.get("task_type") in {"write_script", "generate_project"}) else "modify"
        operation_mode = "safe_create" if mode == "write" else "scoped_modify"

    intent = {
        "version": "v1.20",
        "mode": mode,
        "operation_mode": operation_mode,
        "agent_read_only": bool(agent_read_only),
        "script_read_only": bool(script_read_only),
        "scan_first": bool(scan_first),
        "create_requested": bool(create_requested),
        "fix_requested": bool(fix_requested),
        "modify_requested": bool(modify_requested),
        "analyze_requested": bool(analyze_requested),
        "source_modify_intent": bool(fix_requested or modify_requested),
        "auxiliary_create_intent": bool(create_requested),
        "intent_source": "llm_semantic" if semantic_controls_mode else "heuristic_fallback",
        "create_paths": create_paths,
        "read_reference_paths": read_refs,
        "ambiguous_paths": ambiguous_paths,
        "allowed_modify_paths": allowed_modify_paths,
        "protected_existing_paths": protected_existing_paths,
        "scope_contract": scope_contract,
        "semantic_write_scope": semantic_write_scope,
        "prohibited_artifacts": prohibited_artifacts,
        "path_mentions": path_mentions,
        "llm_task_type": llm_obj.get("task_type"),
        "llm_read_only": llm_obj.get("read_only"),
    }
    return apply_read_only_lock_to_intent(intent, read_only_policy)


def mode_from_intent(intent: dict[str, Any], fallback: str | None = None) -> str:
    return intent.get("mode") or fallback or "modify"


def read_only_from_intent(intent: dict[str, Any]) -> bool:
    return bool(intent.get("agent_read_only"))
