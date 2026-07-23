from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from coding_agent.scope.read_only_policy import detect_global_read_only_lock

# Common relative file path pattern. This is intentionally generic: it extracts
# user-mentioned paths such as scripts/foo.py, tests/test_foo.py, README.md,
# configs/x.yaml, etc. It does not encode any project-specific filenames.
_PATH_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:py|json|jsonl|csv|md|yaml|yml|toml|txt|sh)|[A-Za-z0-9_.-]+\.(?:py|json|jsonl|csv|md|yaml|yml|toml|txt|sh))(?![\w/-]|\.[A-Za-z0-9])")
WRITE_INTENT_ZH = ["新增", "创建", "写", "生成", "实现", "增加", "添加"]
WRITE_INTENT_EN = ["add", "create", "write", "generate", "implement", "new script", "new file"]
NEGATION_WORDS_ZH = ["禁止", "不要", "不得", "不能", "不许", "不允许", "无需", "无须", "只读", "只分析", "只查看"]
NEGATION_WORDS_EN = ["do not", "don't", "dont", "must not", "never", "no ", "without", "read-only", "read only", "only analyze", "only inspect"]


def normalize_rel(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def extract_mentioned_paths(task: str) -> list[str]:
    out: list[str] = []
    seen = set()
    for m in _PATH_RE.finditer(task or ""):
        p = normalize_rel(m.group(1))
        if not p or p.startswith("/") or ".." in Path(p).parts:
            continue
        if p.startswith(".coding_agent/"):
            continue
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def has_unnegated_intent_word(task: str, zh_words: list[str], en_words: list[str], *, window: int = 32) -> bool:
    text = task or ""
    low = text.lower()
    checks: list[tuple[str, str]] = []
    checks.extend((text, w) for w in zh_words)
    checks.extend((low, w.lower()) for w in en_words)
    for haystack, word in checks:
        if not word:
            continue
        start = 0
        while True:
            idx = haystack.find(word, start)
            if idx < 0:
                break
            before = haystack[max(0, idx - window):idx]
            before_low = before.lower()
            negated = any(n in before for n in NEGATION_WORDS_ZH) or any(n in before_low for n in NEGATION_WORDS_EN)
            if not negated:
                return True
            start = idx + max(1, len(word))
    return False


def explicit_global_read_only_requested(task: str) -> bool:
    """True only when the entire task is read-only.

    Scoped constraints like "do not modify training code" or "do not delete
    tests" must not make the whole task read-only when the same task asks to
    create/add/write a new artifact.
    """
    policy = detect_global_read_only_lock(task or "")
    if policy.get("locked"):
        return True
    t = (task or "").lower()
    zh = task or ""
    write_intent = has_unnegated_intent_word(task, WRITE_INTENT_ZH, WRITE_INTENT_EN)
    strong_global = any(x in zh for x in ["完全只读", "只分析这个项目", "只读分析这个项目", "只查看这个项目", "不要写任何"]) or any(
        x in t for x in ["read-only", "read only", "only analyze", "only inspect", "do not write any", "no writes"]
    )
    # If the task asks to create/add/write a file, phrases such as
    # "只读分析脚本" describe the new script's behavior, not the agent's global
    # permission. Scoped protections are handled by protected_globs.
    if write_intent:
        return False
    if strong_global or "只读" in zh:
        return True
    # These are global only when no write intent exists.
    weak_global = any(x in zh for x in ["不要修改", "不修改", "不要写", "不写", "不要改", "不改源码", "不修改源码"]) or any(
        x in t for x in ["do not modify", "don't modify", "do not write", "don't write", "do not edit", "don't edit"]
    )
    return bool(weak_global)


def _protect_globs_from_task(task: str) -> list[str]:
    t = (task or "").lower()
    zh = task or ""
    globs: list[str] = []
    if any(x in zh for x in ["训练代码", "训练脚本", "训练文件"]) or "training code" in t or "train code" in t:
        globs += ["**/train*.py", "**/*train*.py", "scripts/*train*.sh", "scripts/*run*.sh"]
    if any(x in zh for x in ["模型代码", "模型文件"]) or "model code" in t:
        globs += ["**/model*.py", "**/models.py", "**/*model*.py"]
    if any(x in zh for x in ["loss 代码", "loss代码", "损失代码", "损失函数"]) or "loss code" in t:
        globs += ["**/loss*.py", "**/losses.py", "**/*loss*.py"]
    if any(x in zh for x in ["已有测试", "现有测试", "不要删除测试", "不要改测试", "不能改测试"]) or any(
        x in t for x in ["existing tests", "do not delete tests", "do not modify tests", "do not weaken tests"]
    ):
        globs += ["tests/**", "test_*.py", "**/test_*.py", "**/*_test.py"]
    # User-mentioned paths in negative clauses are protected. Keep this generic:
    # any mentioned path near do-not-modify wording should be protected.
    for p in extract_mentioned_paths(task):
        before = task[max(0, task.find(p) - 30): task.find(p)] if p in task else ""
        if any(x in before for x in ["不要修改", "不修改", "不要改", "不改", "禁止修改", "保护"]):
            globs.append(p)
    # de-dupe
    out: list[str] = []
    seen = set()
    for g in globs:
        if g not in seen:
            out.append(g)
            seen.add(g)
    return out


def build_write_scope_policy(task: str, mode: str | None, read_only: bool, file_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    mentioned = extract_mentioned_paths(task)
    planned = [normalize_rel(x.get("path")) for x in (file_plan or {}).get("files", []) if x.get("path")]
    allowed_create_paths: list[str] = []
    for p in mentioned + planned:
        if p and p not in allowed_create_paths:
            allowed_create_paths.append(p)
    protected_globs = _protect_globs_from_task(task)
    return {
        "version": "v1.13",
        "mode": mode,
        "read_only": bool(read_only),
        "allowed_write": (not read_only) and (mode in {"write", "modify", "debug", "generate_project", "repair_existing"}),
        "allowed_create_paths": allowed_create_paths,
        "protected_globs": protected_globs,
        "protect_existing_tests": bool(protected_globs and any("test" in g.lower() for g in protected_globs)) or mode in {"debug", "modify", "repair_existing"},
        "notes": [
            "read_only is global; protected_globs are scoped path protections",
            "creating a planned or user-mentioned new file is allowed when allowed_write=true",
        ],
    }


def path_matches_any(rel: str, patterns: list[str]) -> bool:
    rel = normalize_rel(rel)
    for pat in patterns or []:
        p = normalize_rel(pat)
        if not p:
            continue
        if rel == p or fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.replace("**/", "*/")):
            return True
    return False


def can_write_path(state: dict[str, Any], rel_path: str, *, exists: bool, is_test: bool = False) -> tuple[bool, str, dict[str, Any]]:
    policy = state.get("write_scope_policy") or build_write_scope_policy(
        state.get("task", ""), state.get("mode"), bool(state.get("read_only")), state.get("file_plan")
    )
    rel = normalize_rel(rel_path)
    if policy.get("read_only") or state.get("read_only"):
        return False, "global read-only policy blocks writes", {"write_scope_policy": policy}
    if not policy.get("allowed_write", True):
        return False, "write scope policy does not allow writes", {"write_scope_policy": policy}
    if exists:
        if path_matches_any(rel, policy.get("protected_globs") or []):
            return False, "path is protected by scoped write policy", {"write_scope_policy": policy, "path": rel}
        if is_test and policy.get("protect_existing_tests"):
            return False, "existing test is protected; fix implementation or create new tests", {"write_scope_policy": policy, "path": rel}
        return True, "existing file is writable under current mode policy", {"write_scope_policy": policy, "path": rel}
    allowed = policy.get("allowed_create_paths") or []
    planned = [normalize_rel(x.get("path")) for x in (state.get("file_plan") or {}).get("files", []) if x.get("path")]
    if is_test and not rel.startswith(".coding_agent_test/"):
        return False, "agent-generated verification tests must be written under .coding_agent_test", {"write_scope_policy": policy, "path": rel}
    if not allowed and not planned:
        return True, "new file creation allowed by mode policy", {"write_scope_policy": policy, "path": rel}
    if rel in allowed or rel in planned:
        return True, "new file is explicitly planned or user-mentioned", {"write_scope_policy": policy, "path": rel}
    return False, "new file path was not in allowed_create_paths or file_plan", {"write_scope_policy": policy, "path": rel, "allowed_create_paths": allowed, "planned_paths": planned}
