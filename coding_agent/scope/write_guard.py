from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_agent.core.utils import sha16
from coding_agent.scope.write_scope import normalize_rel, path_matches_any, can_write_path
from coding_agent.memory.artifact_provenance import artifact_record_for_path
from coding_agent.workspace.run_paths import is_test_like_path, run_dir_for

# Generic protected locations and suffixes. These are not project-specific;
# they cover common data/result/checkpoint artifacts that should be read as
# references unless the user explicitly asks to overwrite them.
PROTECTED_DIR_PARTS = {
    "data", "dataset", "datasets", "raw", "processed", "outputs", "output",
    "results", "result", "experiments", "experiment", "checkpoints", "checkpoint",
    "weights", "models", "logs", "log", "wandb", "runs",
}
PROTECTED_SUFFIXES = {
    ".json", ".jsonl", ".csv", ".tsv", ".parquet", ".npz", ".npy",
    ".pt", ".pth", ".ckpt", ".bin", ".pkl", ".pickle", ".h5", ".nc",
}
PROTECTED_NAME_PATTERNS = [
    re.compile(r".*summary.*\.(json|jsonl|csv|md)$", re.I),
    re.compile(r".*metrics.*\.(json|jsonl|csv|txt)$", re.I),
    re.compile(r".*result.*\.(json|jsonl|csv|txt)$", re.I),
    re.compile(r"config\.(json|yaml|yml|toml)$", re.I),
]

CREATE_INTENT_WORDS = ["新增", "创建", "生成", "写入", "写到", "保存到", "输出到", "create", "add", "write", "save", "output"]
READ_INTENT_WORDS = ["读取", "读", "探测", "参考", "schema", "read", "load", "input", "fallback", "回退", "不可用", "如果"]
# Real UTF-8 Chinese keywords, written with escapes so source editing through
# different terminals cannot corrupt intent detection.
CREATE_INTENT_WORDS += [
    "\u65b0\u589e",  # 新增
    "\u521b\u5efa",  # 创建
    "\u65b0\u5efa",  # 新建
    "\u751f\u6210",  # 生成
    "\u5199\u5165",  # 写入
    "\u5199\u5230",  # 写到
    "\u5199\u4e00\u4e2a",  # 写一个
    "\u5199\u4e00\u4e2a\u65b0\u7684",  # 写一个新的
    "\u5b9e\u73b0",  # 实现
    "\u6dfb\u52a0",  # 添加
]
READ_INTENT_WORDS += [
    "\u8bfb\u53d6",  # 读取
    "\u53ea\u8bfb",  # 只读
    "\u53ea\u8bfb\u53d6",  # 只读取
    "\u5e94\u8bfb\u53d6",  # 应读取
    "\u811a\u672c\u5e94\u8bfb\u53d6",  # 脚本应读取
    "\u53c2\u8003",  # 参考
    "\u56de\u9000",  # 回退
    "\u5982\u679c",  # 如果
    "\u4e0d\u53ef\u7528",  # 不可用
]


OUTPUT_INTENT_WORDS = [
    "\u8f93\u51fa",
    "\u5bfc\u51fa",
    "\u4fdd\u5b58",
    "\u4fdd\u5b58\u5230",
    "\u5199\u5165",
    "\u5199\u5230",
    "output",
    "export",
    "save",
    "write",
    "write to",
]
CREATE_INTENT_WORDS += [w for w in OUTPUT_INTENT_WORDS if w not in CREATE_INTENT_WORDS]


def _near_text(task: str, path: str, window: int = 60) -> str:
    if not task or not path or path not in task:
        return ""
    idx = task.find(path)
    return task[max(0, idx - window): idx + len(path) + window]


def _last_marker_position(text: str, words: list[str]) -> int:
    low = text.lower()
    best = -1
    for word in words:
        if not word:
            continue
        best = max(best, text.rfind(word), low.rfind(word.lower()))
    return best


def path_has_output_intent(task: str, path: str) -> bool:
    """True when local wording treats this path as an output artifact."""
    if not task or not path or path not in task:
        return False
    idx = task.find(path)
    before = task[max(0, idx - 80):idx]
    output_pos = _last_marker_position(before, OUTPUT_INTENT_WORDS)
    if output_pos < 0:
        return False
    read_pos = _last_marker_position(before, READ_INTENT_WORDS)
    if output_pos < read_pos:
        return False
    between = before[output_pos:]
    if any(token in between for token in ["\n", "\u3002", "\uff1b", ";"]):
        return False
    if len(between) > 48:
        return False
    return True


def path_has_explicit_create_intent(task: str, path: str) -> bool:
    # Use local context around the path. If the immediate wording says read/load/input,
    # the path is a reference even if the overall task also says create/add a different file.
    # For code/test/readme output paths, creation words before the path win over
    # nearby runtime-read words after the path, e.g. "新增一个只读分析脚本 scripts/foo.py。脚本应读取 data.json".
    if not task or not path or path not in task:
        return False
    idx = task.find(path)
    before_close = task[max(0, idx - 36): idx]
    before_wide = task[max(0, idx - 90): idx]
    low_before_close = before_close.lower()
    low_before_wide = before_wide.lower()
    rel = normalize_rel(path)
    code_like = is_probably_code_output(rel) or is_test_path(rel)
    data_like = Path(rel).suffix.lower() in {".json", ".jsonl", ".csv", ".yaml", ".yml", ".toml"}
    if path_has_output_intent(task, path):
        return True
    if data_like and any(w in before_close or w in low_before_close for w in READ_INTENT_WORDS):
        return False
    create_before = any(w in before_wide or w in low_before_wide for w in CREATE_INTENT_WORDS)
    if any(w in before_close or w in low_before_close for w in READ_INTENT_WORDS):
        if not (code_like and create_before):
            return False
    near = _near_text(task, path)
    low = near.lower()
    return any(w in near or w in low for w in CREATE_INTENT_WORDS)


def path_has_read_reference_intent(task: str, path: str) -> bool:
    if path_has_output_intent(task, path):
        return False
    near = _near_text(task, path)
    if not near:
        return False
    rel = normalize_rel(path)
    idx = task.find(path) if path in task else -1
    before_wide = task[max(0, idx - 90):idx] if idx >= 0 else ""
    low_before_wide = before_wide.lower()
    # A code/test/readme path locally introduced by create/add/write is not a read reference
    # just because later text says the generated script should read another path.
    if (is_probably_code_output(rel) or is_test_path(rel)) and any(w in before_wide or w in low_before_wide for w in CREATE_INTENT_WORDS):
        return False
    low = near.lower()
    return any(w in near or w in low for w in READ_INTENT_WORDS)


def is_test_path(rel: str) -> bool:
    return is_test_like_path(rel)


def is_probably_code_output(rel: str, kind: str | None = None) -> bool:
    rel = normalize_rel(rel)
    p = Path(rel)
    if kind in {"code", "test", "readme"}:
        return True
    if p.suffix == ".py":
        return True
    if p.name.lower().startswith("readme") or p.suffix.lower() in {".md", ".rst"}:
        return True
    return False


def is_protected_existing_file(rel: str) -> bool:
    rel = normalize_rel(rel)
    p = Path(rel)
    parts = {part.lower() for part in p.parts[:-1]}
    suffix = p.suffix.lower()
    name = p.name.lower()
    if suffix in {".pt", ".pth", ".ckpt", ".npz", ".npy", ".parquet", ".nc", ".h5"}:
        return True
    if parts & PROTECTED_DIR_PARTS and suffix in PROTECTED_SUFFIXES:
        return True
    if any(rx.match(name) for rx in PROTECTED_NAME_PATTERNS) and suffix in PROTECTED_SUFFIXES | {".md"}:
        return True
    return False


def classify_plan_item(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(state["workspace"]).resolve()
    task = state.get("task", "")
    rel = normalize_rel(item.get("path"))
    kind = item.get("kind") or "other"
    purpose = str(item.get("purpose") or "")
    full = workspace / rel
    exists = full.exists()
    explicit_create = path_has_explicit_create_intent(task, rel)
    read_reference = path_has_read_reference_intent(task, rel)
    protected_existing = exists and is_protected_existing_file(rel)
    is_test = is_test_path(rel)
    provenance_rec = artifact_record_for_path(workspace, rel)
    historical_agent_artifact = bool(provenance_rec and provenance_rec.get("created_by_agent") and provenance_rec.get("safe_to_modify_by_future_agent"))

    # A user-mentioned existing data/result/config/checkpoint path near read/input
    # wording is a reference, not an output artifact to generate.
    if exists and (protected_existing or read_reference) and not explicit_create:
        # Historical agent-generated scripts/tests may be repaired in a later
        # thread, but existing data/result/config/checkpoint-like files remain
        # read-only references.
        if not (historical_agent_artifact and not protected_existing):
            return {
                **item,
                "path": rel,
                "operation": "read_reference",
                "allowed_to_write": False,
                "exists": True,
                "protected_existing": protected_existing,
                "historical_agent_artifact": historical_agent_artifact,
                "provenance": provenance_rec,
                "reason": "existing protected/reference file should be read, not generated or overwritten",
            }

    if exists:
        ok, reason, details = can_write_path(state, rel, exists=True, is_test=is_test)
        if historical_agent_artifact and not protected_existing and not state.get("read_only"):
            ok = True
            reason = "existing file is historical agent artifact and may be repaired by future agent"
        if protected_existing and not explicit_create:
            ok = False
            reason = "existing data/result/config/checkpoint-like file is protected from overwrite"
        return {
            **item,
            "path": rel,
            "operation": "modify_existing" if ok else "approval_required",
            "allowed_to_write": bool(ok),
            "exists": True,
            "protected_existing": protected_existing,
            "historical_agent_artifact": historical_agent_artifact,
            "provenance": provenance_rec,
            "reason": reason,
            "policy": details.get("write_scope_policy"),
        }

    # New files are allowed when they are plausible deliverables. Bare data files
    # are suspicious unless explicitly requested as output/sample/test fixture.
    if is_probably_code_output(rel, kind) or explicit_create or is_test_like_path(rel):
        ok, reason, details = can_write_path(state, rel, exists=False, is_test=is_test)
        return {
            **item,
            "path": rel,
            "operation": "create_new" if ok else "approval_required",
            "allowed_to_write": bool(ok),
            "exists": False,
            "protected_existing": False,
            "reason": reason,
            "policy": details.get("write_scope_policy"),
        }

    return {
        **item,
        "path": rel,
        "operation": "read_reference",
        "allowed_to_write": False,
        "exists": False,
        "protected_existing": False,
        "reason": "planned data/reference file is not a code/test/readme deliverable and was not explicitly requested as output",
    }


def review_file_plan(state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    reviewed = []
    writable = []
    read_refs = []
    approvals = []
    for item in plan.get("files", []) or []:
        r = classify_plan_item(state, item)
        reviewed.append(r)
        if r.get("allowed_to_write") and r.get("operation") in {"create_new", "modify_existing"}:
            writable.append({k: r.get(k) for k in ("path", "purpose", "kind", "operation") if k in r})
        elif r.get("operation") == "approval_required":
            approvals.append(r)
        else:
            read_refs.append(r)
    return {
        "version": "v1.14",
        "reviewed_files": reviewed,
        "writable_files": writable,
        "read_reference_files": read_refs,
        "approval_required_files": approvals,
        "ok": not approvals,
    }


def _backup_path_for(run_dir: str, rel: str) -> Path:
    safe = normalize_rel(rel).replace("/", "__").replace("\\", "__")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return Path(run_dir) / "backups" / f"{stamp}__{safe}.before"


def prewrite_backup(state: dict[str, Any], rel: str) -> dict[str, Any] | None:
    full = Path(state["workspace"]).resolve() / normalize_rel(rel)
    if not full.exists() or not full.is_file():
        return None
    run_dir = state.get("run_dir") or str(run_dir_for(state["workspace"], state.get("thread_id")))
    backup = _backup_path_for(run_dir, rel)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(full, backup)
    before = full.read_text(encoding="utf-8", errors="replace") if full.stat().st_size < 50_000_000 else ""
    item = {
        "path": normalize_rel(rel),
        "backup_path": str(backup),
        "before_sha16": sha16(before) if before else None,
        "restore_command": f"cp {str(backup)!r} {str(full)!r}",
    }
    manifest_path = Path(run_dir) / "restore_manifest.json"
    try:
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            data = {"version": "v1.14", "backups": []}
        data.setdefault("backups", []).append(item)
        manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    state.setdefault("prewrite_backups", []).append(item)
    state["restore_manifest_path"] = str(manifest_path)
    return item
