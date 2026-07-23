from __future__ import annotations

import re
from typing import Any

from coding_agent.workspace.run_paths import is_test_like_path


def is_test_artifact_path(path: str | None) -> bool:
    return is_test_like_path(path)


def tests_creation_prohibited(task: str | None) -> bool:
    """Detect explicit instructions not to create test artifacts.

    This intentionally targets creation/generation prohibitions. Scoped phrases
    such as "do not modify existing tests" protect existing tests elsewhere and
    should not block creating a new generated test when the user asks for one.
    """
    text = task or ""
    low = text.lower()
    zh_patterns = [
        r"(不要|禁止|不得|不能|不许|不允许|别)(创建|新建|新增|生成|写|添加)[^。；\n]{0,20}(测试|测试文件|test|tests|pytest)",
        r"(无需|无须|不需要)[^。；\n]{0,12}(测试|测试文件|test|tests|pytest)",
        r"(不创建|不生成|不写|不添加)[^。；\n]{0,20}(测试|测试文件|test|tests|pytest)",
    ]
    en_patterns = [
        r"\b(do not|don't|dont|must not|never)\s+(create|add|write|generate)\s+(any\s+)?(?:user-facing\s+|project\s+)?(test|tests|test files|pytest)(?:\s+test files)?\b",
        r"\b(no|without)\s+(test|tests|test files|pytest)\b",
        r"\b(skip)\s+(test|tests|pytest)\b",
        r"\b(test|tests|pytest)\b.{0,40}\b(not required|not needed|not a deliverable|not user-facing)\b",
        r"\b(not required|not needed)\b.{0,40}\b(test|tests|pytest)\b.{0,40}\b(deliverable|user-facing|final output)\b",
    ]
    zh_deliverable_patterns = [
        r"(\u4e0d\u8981\u6c42|\u4e0d\u9700\u8981|\u65e0\u9700|\u65e0\u987b)[^.\n\u3002\uff1b]{0,40}(pytest|test|tests|\u6d4b\u8bd5)[^.\n\u3002\uff1b]{0,40}(\u4ea4\u4ed8|\u7528\u6237|\u6700\u7ec8|\u4fdd\u5b58)",
        r"(pytest|test|tests|\u6d4b\u8bd5)[^.\n\u3002\uff1b]{0,40}(\u4e0d\u4f5c\u4e3a|\u4e0d\u662f|\u4e0d\u9700\u8981)[^.\n\u3002\uff1b]{0,40}(\u4ea4\u4ed8|\u7528\u6237|\u6700\u7ec8|\u4fdd\u5b58)",
    ]
    if any(re.search(p, text, flags=re.I) for p in zh_deliverable_patterns):
        return True
    if any(re.search(p, text, flags=re.I) for p in zh_patterns):
        if not any(x in text for x in ["已有测试", "现有测试"]) and "existing tests" not in low:
            return True
    if any(re.search(p, low, flags=re.I) for p in en_patterns):
        if "existing tests" not in low and "modify existing tests" not in low:
            return True
    return False


# Avoid accidental pytest collection when this helper is imported into a test
# module under its public name.
tests_creation_prohibited.__test__ = False


def detect_prohibited_artifacts(task: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if tests_creation_prohibited(task):
        out.append({
            "kind": "tests",
            "patterns": ["tests/**", "test_*.py", "**/test_*.py", "**/*_test.py"],
            "reason": "user explicitly prohibited creating test artifacts",
        })
    return out


def artifact_kind_for_path(path: str | None) -> str | None:
    if is_test_artifact_path(path):
        return "tests"
    return None


def is_prohibited_artifact_path(path: str | None, prohibited_artifacts: list[dict[str, Any]] | None) -> bool:
    kind = artifact_kind_for_path(path)
    if not kind:
        return False
    return any(item.get("kind") == kind for item in prohibited_artifacts or [])
