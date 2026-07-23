from __future__ import annotations

import re
from typing import Any


_PATH_RE = re.compile(
    r"(?<![\w./-])"
    r"([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\."
    r"(?:py|json|jsonl|csv|md|yaml|yml|toml|txt|sh)|"
    r"[A-Za-z0-9_.-]+\.(?:py|json|jsonl|csv|md|yaml|yml|toml|txt|sh))"
    r"(?![\w./-])"
)

_CLAUSE_SPLIT_RE = re.compile(r"[\n\r。；;!?？]+")

_NEGATION_WORDS_ZH = ["禁止", "不要", "不得", "不能", "不许", "不允许", "无需", "无须"]
_NEGATION_WORDS_EN = ["do not", "don't", "dont", "must not", "never", "without", "no "]

_FUTURE_OR_HYPOTHETICAL_ZH = [
    "后续",
    "以后",
    "将来",
    "未来",
    "下一步",
    "后面",
    "如果后续",
    "如果以后",
]
_FUTURE_OR_HYPOTHETICAL_EN = ["later", "future", "next step", "if later", "if needed later"]

_CURRENT_WRITE_WORDS_ZH = [
    "允许创建",
    "可以创建",
    "只允许创建",
    "创建新文件",
    "创建新脚本",
    "新增",
    "创建",
    "新建",
    "生成",
    "写一个",
    "写个",
    "实现",
    "添加",
    "修改",
    "修复",
    "重构",
]
_CURRENT_WRITE_WORDS_EN = [
    "allowed to create",
    "may create",
    "create",
    "add",
    "write",
    "generate",
    "implement",
    "modify",
    "fix",
    "repair",
    "refactor",
]

_GLOBAL_READ_ONLY_PATTERNS_ZH = [
    "本轮任务是只读",
    "本轮只读",
    "只读深度理解",
    "只读分析",
    "只读理解",
    "完全只读",
    "只允许读取",
    "只能读取",
    "不能进行任何写操作",
    "不进行任何写操作",
    "禁止创建、修改、删除任何文件",
    "禁止创建、修改、删除",
    "禁止创建、修改或删除",
    "禁止创建修改删除",
    "禁止写任何文件",
    "禁止写入任何文件",
    "禁止创建任何文件",
    "禁止修改任何文件",
]
_GLOBAL_READ_ONLY_PATTERNS_EN = [
    "read-only",
    "read only",
    "only analyze",
    "only inspect",
    "do not write any files",
    "do not modify any files",
    "must not write any files",
    "no writes",
    "without writing files",
]


def _contains_any(text: str, zh_words: list[str], en_words: list[str]) -> bool:
    low = (text or "").lower()
    return any(w in (text or "") for w in zh_words) or any(w in low for w in en_words)


def _has_negation_before(text: str, idx: int, *, window: int = 24) -> bool:
    before = text[max(0, idx - window):idx]
    low = before.lower()
    return any(w in before for w in _NEGATION_WORDS_ZH) or any(w in low for w in _NEGATION_WORDS_EN)


def _clause_has_current_write_intent(clause: str) -> bool:
    low = clause.lower()
    if _contains_any(clause, _FUTURE_OR_HYPOTHETICAL_ZH, _FUTURE_OR_HYPOTHETICAL_EN):
        return False

    # Permission clauses such as "本轮允许创建新文件" are explicit current write intent.
    if any(w in clause for w in ["允许创建", "可以创建", "只允许创建", "允许修改", "只允许修改"]):
        return True
    if any(w in low for w in ["allowed to create", "may create", "only modify", "allowed to modify"]):
        return True

    has_path = bool(_PATH_RE.search(clause))
    for word in _CURRENT_WRITE_WORDS_ZH:
        start = 0
        while True:
            idx = clause.find(word, start)
            if idx < 0:
                break
            if not _has_negation_before(clause, idx) and has_path:
                return True
            start = idx + max(1, len(word))
    for word in _CURRENT_WRITE_WORDS_EN:
        start = 0
        while True:
            idx = low.find(word, start)
            if idx < 0:
                break
            if not _has_negation_before(low, idx) and has_path:
                return True
            start = idx + max(1, len(word))
    return False


def has_explicit_current_write_intent(task: str) -> bool:
    """Strict current-run write detector.

    This intentionally ignores future/hypothetical wording such as "如果后续要新增".
    It is used only to decide whether a global read-only prohibition may be
    relaxed; ordinary task intent classification can still detect broader write
    requests elsewhere.
    """
    for clause in _CLAUSE_SPLIT_RE.split(task or ""):
        if _clause_has_current_write_intent(clause):
            return True
    return False


def detect_global_read_only_lock(task: str) -> dict[str, Any]:
    text = task or ""
    low = text.lower()
    evidence: list[str] = []
    for pat in _GLOBAL_READ_ONLY_PATTERNS_ZH:
        if pat in text:
            evidence.append(pat)
    for pat in _GLOBAL_READ_ONLY_PATTERNS_EN:
        if pat in low:
            evidence.append(pat)

    # "只读" alone is strong when there is no explicit current write permission.
    if "只读" in text and "只读" not in evidence:
        evidence.append("只读")

    explicit_write = has_explicit_current_write_intent(text)
    locked = bool(evidence) and not explicit_write
    reason = ""
    if locked:
        reason = "global read-only/no-write instruction without an explicit current write target"
    elif evidence and explicit_write:
        reason = "read-only wording is scoped or overridden by explicit current write permission"
    else:
        reason = "no global read-only/no-write instruction detected"

    return {
        "version": "v1.20",
        "locked": locked,
        "explicit_current_write_intent": explicit_write,
        "evidence": evidence,
        "reason": reason,
    }


def apply_read_only_lock_to_intent(intent: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    out = dict(intent or {})
    out["read_only_policy"] = policy
    out["write_locked"] = bool(policy.get("locked"))
    if not policy.get("locked"):
        return out

    out.update({
        "mode": "analyze",
        "operation_mode": "read_only_analysis",
        "agent_read_only": True,
        "script_read_only": False,
        "create_requested": False,
        "fix_requested": False,
        "modify_requested": False,
    })

    read_refs = list(out.get("read_reference_paths") or [])
    for p in out.get("create_paths") or []:
        if p not in read_refs:
            read_refs.append(p)
    out["read_reference_paths"] = read_refs
    out["create_paths"] = []
    for mention in out.get("path_mentions") or []:
        if mention.get("intent") == "create_target":
            mention["intent"] = "read_reference"
            mention["read_reference"] = True
            mention["explicit_create"] = False
            mention["read_only_lock_override"] = True
    return out
