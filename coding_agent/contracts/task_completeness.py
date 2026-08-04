from __future__ import annotations

import re
from typing import Any


TASK_COMPLETENESS_VERSION = "v1.0"

_INSPECT_ZH = ("分析", "讲解", "总结", "查看", "看看", "看一下", "看下", "了解", "审查", "扫描")
_INSPECT_EN = ("analyze", "inspect", "explain", "summarize", "review", "look at", "understand", "scan")
_CREATE_ZH = ("写一个", "写个", "创建", "新建", "生成", "做一个", "做个", "实现")
_CREATE_EN = ("write", "create", "build", "generate", "implement", "make")
_REPAIR_ZH = ("修复", "修一下", "修一修", "修个", "排查", "调试", "报错", "跑不通", "失败")
_REPAIR_EN = ("fix", "repair", "debug", "failing", "broken", "error", "traceback")
_MODIFY_ZH = ("添加", "新增", "增加", "加一个", "加个", "修改", "重构", "改成", "加入")
_MODIFY_EN = ("add", "modify", "change", "refactor", "patch", "extend")
_ARTIFACT_ZH = ("脚本", "工具", "程序", "应用", "项目", "命令行", "功能")
_ARTIFACT_EN = ("script", "tool", "program", "app", "application", "project", "cli", "feature")
_LANGUAGE_MARKERS = (
    "python", "py", "javascript", "typescript", "node", "rust", "go", "java", "kotlin",
    "swift", "ruby", "php", "c++", "c#", "shell", "bash", "powershell",
)
_SUFFIX_LANGUAGES = {
    ".py": "python",
    ".sh": "POSIX shell",
    ".bash": "bash",
    ".js": "javascript",
    ".ts": "typescript",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
}


def _contains(text: str, zh: tuple[str, ...], en: tuple[str, ...]) -> bool:
    low = text.casefold()
    return any(word in text for word in zh) or any(
        re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", low)
        for word in en
    )


def _activity(task: str, intent: dict[str, Any], *, workspace_nonempty: bool) -> str:
    operation_mode = str(intent.get("operation_mode") or "")
    mode = str(intent.get("mode") or "")
    # In an empty workspace, a resolved safe-create intent is authoritative.
    # Command names such as "add" may otherwise look like requests to modify
    # an existing repository even when the user clearly asked to build a new
    # CLI or project.
    if not workspace_nonempty and (
        operation_mode == "safe_create"
        or (mode in {"write", "generate_project"} and intent.get("create_requested"))
    ):
        return "create"
    if _contains(task, _REPAIR_ZH, _REPAIR_EN):
        return "repair"
    if _contains(task, _MODIFY_ZH, _MODIFY_EN):
        return "modify"
    if _contains(task, _CREATE_ZH, _CREATE_EN):
        return "create"
    if _contains(task, _INSPECT_ZH, _INSPECT_EN):
        return "inspect"
    if operation_mode == "read_only_analysis" or mode == "analyze":
        return "inspect"
    if operation_mode == "safe_create" or mode in {"write", "generate_project"}:
        return "create"
    if mode == "debug":
        return "repair"
    return "modify"


def _semantic_remainder(task: str, activity: str) -> str:
    text = task.casefold()
    phrases: tuple[str, ...] = (
        *_CREATE_ZH, *_CREATE_EN, *_REPAIR_ZH, *_REPAIR_EN,
        *_MODIFY_ZH, *_MODIFY_EN, *_INSPECT_ZH, *_INSPECT_EN,
        *_ARTIFACT_ZH, *_ARTIFACT_EN,
        "请", "帮我", "麻烦", "一下", "这个", "当前", "现有", "代码", "仓库",
        "东西", "某个东西", "随便", "问题", "错误", "please", "for me", "this", "the", "a", "an",
        "something", "anything", "it", "bug", "issue", "code", "repository", "repo",
    )
    for phrase in sorted(set(phrases), key=len, reverse=True):
        text = text.replace(phrase.casefold(), " ")
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    # Repairing "the project" without a failure symptom is still underspecified.
    if activity == "repair":
        for generic in ("项目", "工程", "project", "workspace"):
            text = text.replace(generic, "")
    return text


def _repo_facts(repo_map: dict[str, Any]) -> tuple[bool, bool, list[str]]:
    files = [str(item) for item in repo_map.get("files") or []]
    project_types = [str(item) for item in repo_map.get("project_types") or []]
    return bool(files), bool(repo_map.get("has_tests")), project_types


def _assumption(assumption_id: str, field: str, value: Any, rationale: str, source: str) -> dict[str, Any]:
    return {
        "id": assumption_id,
        "field": field,
        "value": value,
        "rationale": rationale,
        "source": source,
    }


def _question(question_id: str, question: str, reason: str) -> dict[str, Any]:
    return {"id": question_id, "question": question, "reason": reason, "required": True}


def _implementation_contract(activity: str, task: str, assumptions: list[dict[str, Any]]) -> dict[str, Any]:
    requirements: list[dict[str, Any]] = []
    if activity == "create":
        requirements.append({
            "id": "representative_execution",
            "kind": "behavior",
            "scope": "implementation",
            "description": "The delivered implementation runs successfully for a representative invocation consistent with the stated objective.",
            "required": True,
            "evidence_mode": "execution",
            "verification_hint": "Run the main public entry point with a small representative input and check its observable result.",
        })
    elif activity in {"modify", "repair"}:
        requirements.append({
            "id": "requested_change_execution",
            "kind": "behavior",
            "scope": "implementation",
            "description": "The resulting project passes an execution-based check relevant to the requested change.",
            "required": True,
            "evidence_mode": "execution",
            "verification_hint": "Execute the narrowest available check that demonstrates the requested behavior or repaired failure.",
        })
    if re.search(r"(?i)(?:\bcli\b|command[- ]line|命令行)", task):
        requirements.append({
            "id": "usable_cli_invocation",
            "kind": "behavior",
            "scope": "implementation",
            "description": "The command-line entry point can be invoked successfully through a public execution path.",
            "required": True,
            "evidence_mode": "execution",
            "verification_hint": "Invoke one representative public CLI command and check its observable result.",
        })
    return {
        "version": "implementation_contract_v1",
        "source": "agent_defaults",
        "requirements": requirements,
        "assumption_ids": [item["id"] for item in assumptions],
    }


def assess_task_completeness(
    task: str,
    task_spec: dict[str, Any] | None = None,
    task_intent: dict[str, Any] | None = None,
    repo_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether execution can safely continue after repository discovery.

    Missing low-risk implementation details become explicit assumptions. Missing
    information that changes the core behavior becomes a user question.
    """
    task = str(task or "").strip()
    task_spec = dict(task_spec or {})
    task_intent = dict(task_intent or {})
    repo_map = dict(repo_map or {})
    workspace_nonempty, has_tests, project_types = _repo_facts(repo_map)
    activity = _activity(task, task_intent, workspace_nonempty=workspace_nonempty)
    remainder = _semantic_remainder(task, activity)
    # The original task text is authoritative for core behavior. Intake may
    # legitimately infer an artifact path from a vague request (for example
    # script.py), but that does not answer what the artifact should do.
    behavior_clear = bool(remainder)
    assumptions: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    missing: list[str] = []
    discoverable: list[str] = []

    if activity == "inspect":
        behavior_clear = True
        assumptions.append(_assumption(
            "inspection_read_only", "write_policy", "read_only",
            "Inspection requests should not modify project source files.", "safe_default",
        ))
        if workspace_nonempty:
            discoverable.extend(["project structure", "entry points", "test layout"])
    elif activity == "create":
        if not behavior_clear:
            missing.append("core behavior")
            questions.append(_question(
                "core_behavior",
                "希望它完成什么核心功能？请说明最重要的输入、输出或操作。",
                "不同答案会产生完全不同的实现，不能用安全默认值代替。",
            ))
        else:
            low = task.casefold()
            if not any(marker in low for marker in _LANGUAGE_MARKERS):
                inferred_paths = [str(path) for path in task_intent.get("create_paths") or []]
                inferred_language = next(
                    (
                        language
                        for path in inferred_paths
                        for suffix, language in _SUFFIX_LANGUAGES.items()
                        if path.casefold().endswith(suffix)
                    ),
                    None,
                )
                language = inferred_language or (project_types[0] if workspace_nonempty and project_types else "python")
                source = "intake_inference" if inferred_language else ("repository" if workspace_nonempty and project_types else "safe_default")
                assumptions.append(_assumption(
                    "implementation_language", "language", language,
                    "No language was specified; use the already resolved artifact type, the repository's dominant type, or Python for a new standalone utility.",
                    source,
                ))
            if not task_intent.get("create_paths"):
                assumptions.append(_assumption(
                    "minimal_layout", "output_layout", "minimal runnable layout in the workspace",
                    "No output path was specified, so choose the smallest conventional layout consistent with the objective.",
                    "safe_default",
                ))
    elif activity == "repair":
        if not behavior_clear:
            missing.append("failure symptom")
            if has_tests:
                discoverable.append("failing existing tests")
                assumptions.append(_assumption(
                    "repair_from_existing_tests", "diagnostic_start", "run existing tests",
                    "The repository has tests that can identify the failure without guessing desired behavior.", "repository",
                ))
                behavior_clear = True
            else:
                questions.append(_question(
                    "failure_symptom",
                    "目前具体哪里出错了？请提供报错、失败命令或期望与实际结果。",
                    "仓库中没有可用于自动定位问题的现有测试。",
                ))
        else:
            discoverable.extend(["relevant implementation", "existing verification commands"])
    else:
        if not behavior_clear:
            missing.append("requested change")
            questions.append(_question(
                "requested_change",
                "希望修改成什么结果？请说明最重要的行为变化或验收方式。",
                "当前描述不足以区分可能的修改方向。",
            ))
        elif workspace_nonempty:
            discoverable.extend(["change location", "existing conventions", "relevant tests"])
        else:
            missing.append("target repository content")
            questions.append(_question(
                "target_content",
                "要修改的代码还不在工作区中；请提供目标文件或改为描述要新建的内容。",
                "修改任务必须先有可定位的现有实现。",
            ))

    decision = "clarify" if questions else "proceed"
    if not questions and discoverable:
        decision = "inspect_then_proceed"
    if not questions and activity != "inspect":
        assumptions.append(_assumption(
            "representative_verification", "acceptance", "execution-based representative check",
            "No complete acceptance procedure was supplied; verify the narrowest observable behavior consistent with the task.",
            "safe_default",
        ))

    implementation_contract = _implementation_contract(activity, task, assumptions)
    return {
        "version": TASK_COMPLETENESS_VERSION,
        "stage": "post_repo_scan",
        "activity": activity,
        "objective_clarity": "sufficient" if task else "missing",
        "target_clarity": "repository_discoverable" if workspace_nonempty else ("new_artifact" if activity == "create" else "missing"),
        "behavior_clarity": "sufficient" if behavior_clear else "missing",
        "acceptance_clarity": "explicit" if task_spec.get("success_criteria") else "defaulted",
        "missing_information": missing,
        "discoverable_from_repository": discoverable,
        "assumptions": assumptions,
        "questions": questions,
        "decision": decision,
        "confidence": 0.9 if not questions else 0.98,
        "implementation_contract": implementation_contract,
        "implementation_requirements": implementation_contract["requirements"],
    }
