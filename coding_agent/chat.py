from __future__ import annotations

import argparse
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.core.utils import read_json, write_json
from coding_agent.core.resume import prepare_resumed_state
from coding_agent.export_audit import export_audit_bundle
from coding_agent.graph import build_graph
from coding_agent.workspace.run_paths import agent_runs_root, agent_test_root_rel, run_dir_for, safe_id
from coding_agent.ux.chat_store import (
    append_chat_turn,
    chat_session_paths,
    create_chat_session,
    default_chat_store_dir,
    list_chat_sessions,
    load_chat_history,
    load_chat_meta,
    resolve_chat_session_choice,
)
from coding_agent.ux.human_report import build_human_report, write_human_report
from coding_agent.ux.language import language_instruction_for_text, response_language_quality
from coding_agent.ux.task_defaults import prepare_task_for_agent, should_auto_route_chat_to_code
from coding_agent.ux.token_usage import summarize_token_usage

try:  # Rich is optional; the plain ANSI fallback keeps server installs simple.
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except Exception:  # pragma: no cover - exercised when rich is not installed
    Console = None
    Panel = None
    Table = None
    Text = None

try:  # Optional: enables slash-command completion and a nicer input prompt.
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
except Exception:  # pragma: no cover - optional dependency fallback
    PromptSession = None
    WordCompleter = None


ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "gray": "\033[90m",
}

SLASH_COMMANDS = [
    "/ask",
    "/chat",
    "/code",
    "/repair",
    "/multi",
    "/sessions",
    "/continue",
    "/new",
    "/help",
    "/exit",
    "/quit",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive terminal UI for coding_agent.")
    parser.add_argument("--workspace", help="Workspace directory. If omitted, the UI asks for it.")
    parser.add_argument("--task", help="Run one task non-interactively and exit.")
    parser.add_argument("--task-file", help="Read one task from a file and exit.")
    parser.add_argument("--thread-id", help="Use a fixed thread id. Interactive mode appends the turn number.")
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--repair-existing", action="store_true", help="Default this turn to repair_existing mode.")
    parser.add_argument("--audit-dir", help="Directory for exported audit zips. Defaults to AGENT_AUDIT_DIR or the agent-owned .agent_runs/audits directory.")
    parser.add_argument("--no-audit", action="store_true", help="Do not export an audit zip automatically.")
    parser.add_argument("--include-workspace-audit", action="store_true", help="Include source workspace files in audit zip.")
    parser.add_argument("--show-generated-tests", action="store_true", help="Show generated test file paths in the human report.")
    parser.add_argument("--chat-store-dir", help="Directory for saved terminal chat sessions. Defaults to ./ .coding_agent_chats.")
    parser.add_argument("--continue-chat", nargs="?", const="latest", help="Resume a saved chat session by id or number. Defaults to latest.")
    parser.add_argument("--debug", action="store_true", help="Print exception tracebacks and extra artifact paths.")
    parser.add_argument("--no-clean-state", action="store_true", help="Do not delete this thread's .agent_runs record or .coding_agent_test files before running.")
    return parser.parse_args()


class TerminalUI:
    def __init__(self) -> None:
        self.console = Console() if Console else None

    def banner(self, workspace: Path, model: str, base_url: str) -> None:
        text = f"Coding Agent\nWorkspace: {workspace}\nModel: {model}\nBase URL: {base_url}"
        if self.console and Panel:
            self.console.print(Panel(text, title="[bold cyan]Terminal UX[/bold cyan]", border_style="cyan"))
        else:
            print(color("Coding Agent", "cyan", bold=True))
            print(color(f"Workspace: {workspace}", "gray"))
            print(color(f"Model: {model}", "gray"))
            print(color(f"Base URL: {base_url}", "gray"))

    def read_command(self, current_mode: str, chat_title: str | None = None) -> str:
        width = max(48, min(shutil.get_terminal_size((100, 24)).columns, 120))
        label = f" agent [{current_mode}] "
        if chat_title:
            label += f"{chat_title[:32]} "
        top = "╭" + label + "─" * max(0, width - len(label) - 2) + "╮"
        bottom = "╰" + "─" * max(0, width - 2) + "╯"
        print(color(top, "cyan"))
        prompt = "│ agent> "
        try:
            if PromptSession and WordCompleter:
                completer = WordCompleter(SLASH_COMMANDS, ignore_case=True, sentence=True)
                session = PromptSession(completer=completer, complete_while_typing=True)
                raw = session.prompt(prompt)
            else:
                raw = input(color(prompt, "green", bold=True))
        finally:
            print(color(bottom, "cyan"))
        return raw.strip()

    def info(self, message: str) -> None:
        if self.console:
            self.console.print(f"[cyan]{message}[/cyan]")
        else:
            print(color(message, "cyan"))

    def warn(self, message: str) -> None:
        if self.console:
            self.console.print(f"[yellow]{message}[/yellow]")
        else:
            print(color(message, "yellow"))

    def error(self, message: str) -> None:
        if self.console:
            self.console.print(f"[red]{message}[/red]")
        else:
            print(color(message, "red"))

    def user_message(self, message: str, *, title: str = "User") -> None:
        body = message.strip() or "(empty)"
        if self.console and Panel:
            self.console.print(Panel(body, title=f"[bold cyan]{title}[/bold cyan]", border_style="cyan"))
        else:
            print(color(f"{title}:", "cyan", bold=True))
            print(body)

    def assistant_message(self, message: str, *, title: str = "Assistant") -> None:
        body = message.strip() or "(empty)"
        if self.console and Panel:
            self.console.print(Panel(body, title=f"[bold green]{title}[/bold green]", border_style="green"))
        else:
            print(color(f"{title}:", "green", bold=True))
            print(body)

    def mode_help(self, current_mode: str) -> None:
        text = (
            f"当前模式：{current_mode}\n\n"
            "可选模式：\n"
            "- chat：普通聊天，默认模式；默认是空白新聊天，不扫描/修改项目文件。\n"
            "- code：完整 Coding Agent 任务；用于读项目、写代码、改文件、验证和导出 audit。\n"
            "- repair：修复模式；用于验证并修复已有工作区。\n\n"
            "命令：\n"
            "- /ask 或 /chat：切换到普通聊天模式，之后直接输入问题即可。\n"
            "- /code：切换到 Coding Agent 模式，之后输入的内容会作为代码任务执行。\n"
            "- /repair：切换到修复模式。\n"
            "- /sessions：查看本地保存过的聊天会话。\n"
            "- /continue：恢复最近一次聊天；也支持 /continue 1 或 /continue chat_xxx。\n"
            "- /new：新建一个普通聊天会话。\n"
            "- /exit：退出当前模式；在 chat 中会结束当前上下文并进入空白新聊天。\n"
            "- /quit：退出整个 Agent 程序，并显示本次会话 token 汇总。\n"
            "- /code 你的任务：立即执行一次代码任务。\n"
            "- /repair 你的任务：立即执行一次修复任务。\n"
            "- /multi：多行输入，单独一行输入 . 结束。\n"
            "- /help：查看帮助。"
        )
        if self.console and Panel:
            self.console.print(Panel(text, title="[bold cyan]Options / 可选模式[/bold cyan]", border_style="cyan"))
        else:
            print(color("Options / 可选模式", "cyan", bold=True))
            print(text)

    def mode_changed(self, mode: str) -> None:
        if self.console and Panel:
            self.console.print(Panel(f"已切换到模式：{mode}", title="[bold blue]Mode / 模式[/bold blue]", border_style="blue"))
        else:
            print(color(f"已切换到模式：{mode}", "blue", bold=True))

    def print_chat_sessions(self, sessions: list[dict[str, Any]]) -> None:
        if not sessions:
            self.warn("还没有保存过的聊天会话。")
            return
        if self.console and Table:
            table = Table(title="Saved Chats / 已保存对话", show_header=True, header_style="bold cyan")
            table.add_column("#")
            table.add_column("Title / 标题")
            table.add_column("Turns")
            table.add_column("Updated")
            table.add_column("ID")
            for idx, item in enumerate(sessions, start=1):
                table.add_row(
                    str(idx),
                    str(item.get("title") or "新对话"),
                    str(item.get("turns") or 0),
                    str(item.get("updated_at") or ""),
                    str(item.get("id") or ""),
                )
            self.console.print(table)
            return
        print(color("Saved Chats / 已保存对话", "cyan", bold=True))
        for idx, item in enumerate(sessions, start=1):
            print(f"{idx}. {item.get('title') or '新对话'} | turns={item.get('turns') or 0} | id={item.get('id')}")

    def active_chat(self, meta: dict[str, Any]) -> None:
        text = (
            f"会话：{meta.get('title') or '新对话'}\n"
            f"id：{meta.get('id')}\n"
            f"turns：{meta.get('turns') or 0}\n"
            "普通输入会继续这个上下文；/new 新建会话；/sessions 查看历史；/continue 选择历史会话。"
        )
        if self.console and Panel:
            self.console.print(Panel(text, title="[bold green]Chat Session / 对话会话[/bold green]", border_style="green"))
        else:
            print(color("Chat Session / 对话会话", "green", bold=True))
            print(text)

    def exit_summary(self, session_totals: dict[str, int]) -> None:
        text = (
            "本次会话已结束。\n\n"
            "下次可用选项：\n"
            "- 默认直接输入：空白普通聊天，不自动恢复历史。\n"
            "- /code：执行 Coding Agent 任务，支持读项目、写代码、验证和导出 audit。\n"
            "- /repair：验证并修复已有工作区。\n"
            "- /sessions：查看保存过的聊天会话。\n"
            "- /continue：恢复历史聊天会话。\n"
            "- /help：查看帮助。\n\n"
            f"本次会话总 token：{int(session_totals.get('total_tokens', 0) or 0)}"
        )
        if self.console and Panel:
            self.console.print(Panel(text, title="[bold yellow]Exit / 退出[/bold yellow]", border_style="yellow"))
        else:
            print(color("Exit / 退出", "yellow", bold=True))
            print(text)

    def print_result(
        self,
        final: dict[str, Any],
        *,
        audit_zip: Path | None,
        session_totals: dict[str, int],
        show_generated_tests: bool,
        debug: bool,
    ) -> None:
        report = build_human_report(final, show_generated_tests=show_generated_tests)
        ok = bool(report["ok"])
        title = "任务成功" if ok else "任务失败"
        style = "green" if ok else "red"
        if self.console and Panel:
            body = "\n".join(
                [
                    f"outcome: {report['outcome']}",
                    f"stopped_reason: {report['stopped_reason']}",
                    f"mode: {report['mode']}",
                    f"rounds: {report['round_idx']}",
                    f"verification_ok: {report['verification_ok']}",
                    f"contract_ok: {report['contract_ok']}",
                ]
            )
            self.console.print(Panel(body, title=f"[bold {style}]{title}[/bold {style}]", border_style=style))
        else:
            print(color(title, style, bold=True))
            print(f"outcome: {report['outcome']}")
            print(f"stopped_reason: {report['stopped_reason']}")
            print(f"mode: {report['mode']}, rounds: {report['round_idx']}")
            print(f"verification_ok: {report['verification_ok']}, contract_ok: {report['contract_ok']}")

        if report.get("answer_summary"):
            self._print_answer_summary(str(report["answer_summary"]))
        self._print_files(report, final)
        self._print_verification(report)
        self._print_token_usage(report.get("token_usage") or {}, session_totals)
        self._print_artifacts(final, audit_zip, debug=debug)

    def _print_answer_summary(self, summary: str) -> None:
        if self.console and Panel:
            self.console.print(Panel(summary, title="[bold green]Answer Summary / 结果摘要[/bold green]", border_style="green"))
            return
        print(color("Answer Summary / 结果摘要", "green", bold=True))
        print(summary)

    def _print_files(self, report: dict[str, Any], final: dict[str, Any]) -> None:
        rows = [
            ("修改源文件", report["source_modified_files"]),
            ("新增源文件", report["source_added_files"]),
            ("Agent 内部文件", report["agent_internal_files"]),
        ]
        if report["generated_test_files"]:
            rows.append(("生成测试文件", report["generated_test_files"]))
        if self.console and Table:
            table = Table(title="Files", show_header=True, header_style="bold cyan")
            table.add_column("类别")
            table.add_column("文件")
            for label, paths in rows:
                if not paths:
                    table.add_row(label, "none")
                else:
                    table.add_row(label, "\n".join(paths))
            if report["hidden_generated_tests"]:
                table.add_row("内部生成测试", f"{report['generated_test_count']} 个，默认隐藏")
            self.console.print(table)
            return
        print(color("Files", "blue", bold=True))
        for label, paths in rows:
            print(f"- {label}:")
            if paths:
                for path in paths:
                    print(f"  - {path}")
            else:
                print("  - none")
        if report["hidden_generated_tests"]:
            print(f"- 内部生成测试: {report['generated_test_count']} 个，默认隐藏")

    def _print_verification(self, report: dict[str, Any]) -> None:
        tests = report["test_summary"]
        atom = report["requirement_atom_summary"] or {}
        lines = [
            f"tests: total={tests['total']}, passed={tests['passed']}, failed={tests['failed']}, errors={tests['errors']}",
            f"requirement_atoms: failed={atom.get('required_failed', 0)}, unverified={atom.get('required_unverified', 0)}, total={atom.get('required_total', atom.get('total', 0))}",
            f"final_gate_failures: {report['final_gate_failures']}",
        ]
        if self.console and Panel:
            self.console.print(Panel("\n".join(lines), title="[bold blue]Verification[/bold blue]", border_style="blue"))
        else:
            print(color("Verification", "blue", bold=True))
            for line in lines:
                print(f"- {line}")

    def _print_token_usage(self, token_usage: dict[str, Any], session_totals: dict[str, int]) -> None:
        totals = token_usage.get("totals") or {}
        by_purpose = token_usage.get("by_purpose") or {}
        if self.console and Table:
            table = Table(title="Token Usage", show_header=True, header_style="bold magenta")
            for col in ["Scope", "Calls", "Input", "Output", "Reasoning", "Total", "Cache Hit", "Cache Miss"]:
                justify = "left" if col == "Scope" else "right"
                table.add_column(col, justify=justify)
            self._add_token_row(table, "THIS TURN", totals)
            for purpose, usage in sorted(by_purpose.items()):
                self._add_token_row(table, str(purpose), usage)
            self._add_token_row(table, "SESSION TOTAL", session_totals)
            self.console.print(table)
            return
        print(color("Token Usage", "cyan", bold=True))
        print_token_line("THIS TURN", totals)
        for purpose, usage in sorted(by_purpose.items()):
            print_token_line(str(purpose), usage)
        print_token_line("SESSION TOTAL", session_totals)

    def print_chat_token_usage(self, turn_tokens: dict[str, Any], session_totals: dict[str, int]) -> None:
        token_usage = {"totals": turn_tokens, "by_purpose": {"direct_chat": turn_tokens}}
        self._print_token_usage(token_usage, session_totals)

    def _add_token_row(self, table: Any, label: str, usage: dict[str, Any]) -> None:
        table.add_row(
            label,
            str(int(usage.get("calls", 0) or 0)),
            str(int(usage.get("prompt_tokens", 0) or 0)),
            str(int(usage.get("completion_tokens", 0) or 0)),
            str(int(usage.get("reasoning_tokens", 0) or 0)),
            str(int(usage.get("total_tokens", 0) or 0)),
            str(int(usage.get("prompt_cache_hit_tokens", 0) or 0)),
            str(int(usage.get("prompt_cache_miss_tokens", 0) or 0)),
        )

    def _print_artifacts(self, final: dict[str, Any], audit_zip: Path | None, *, debug: bool) -> None:
        artifacts = final.get("artifacts") or {}
        lines = [
            f"human_report: {artifacts.get('human_report')}",
            f"final_json: {final.get('final_path') or artifacts.get('final_json')}",
        ]
        if audit_zip:
            lines.append(f"audit_zip: {audit_zip}")
        if debug:
            for key in ["trace", "messages", "state_snapshot", "context_pack"]:
                if artifacts.get(key):
                    lines.append(f"{key}: {artifacts[key]}")
        if self.console and Panel:
            self.console.print(Panel("\n".join(str(x) for x in lines if x), title="[bold gray]Artifacts[/bold gray]", border_style="white"))
        else:
            print(color("Artifacts", "gray", bold=True))
            for line in lines:
                if line:
                    print(f"- {line}")


def color(text: str, name: str, *, bold: bool = False) -> str:
    prefix = ANSI.get(name, "")
    if bold:
        prefix = ANSI["bold"] + prefix
    return f"{prefix}{text}{ANSI['reset']}" if prefix else text


def print_token_line(label: str, usage: dict[str, Any]) -> None:
    print(
        f"- {label}: calls={int(usage.get('calls', 0) or 0)}, "
        f"input={int(usage.get('prompt_tokens', 0) or 0)}, "
        f"output={int(usage.get('completion_tokens', 0) or 0)}, "
        f"reasoning={int(usage.get('reasoning_tokens', 0) or 0)}, "
        f"total={int(usage.get('total_tokens', 0) or 0)}"
    )


def empty_session_totals() -> dict[str, int]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "total_tokens": 0,
    }


def add_token_totals(total: dict[str, int], usage: dict[str, Any]) -> None:
    for key in total:
        total[key] = int(total.get(key, 0) or 0) + int(usage.get(key, 0) or 0)


def token_usage_delta(after: dict[str, Any], before: dict[str, Any]) -> dict[str, int]:
    out = empty_session_totals()
    after_totals = after.get("totals") or {}
    before_totals = before.get("totals") or {}
    for key in out:
        out[key] = max(0, int(after_totals.get(key, 0) or 0) - int(before_totals.get(key, 0) or 0))
    return out


def default_audit_dir(workspace: Path, arg_value: str | None) -> Path:
    if arg_value:
        return Path(arg_value).expanduser().resolve()
    import os

    env_value = os.getenv("AGENT_AUDIT_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return agent_runs_root() / "audits"


def make_thread_id(prefix: str | None, turn_idx: int, *, interactive: bool) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = prefix or f"chat_{stamp}"
    if interactive:
        base = f"{base}_{turn_idx:03d}"
    return safe_id(base)


def load_task_from_args(args: argparse.Namespace) -> str | None:
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8")
    return args.task


def run_agent_task(
    *,
    workspace: Path,
    task: str,
    task_runtime_instructions: str = "",
    thread_id: str,
    max_rounds: int,
    repair_existing: bool,
    clean_state: bool,
    resume: bool = False,
    clarification_answer: str | None = None,
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    run_dir = run_dir_for(workspace, thread_id)
    if clean_state and not resume and run_dir.exists():
        shutil.rmtree(run_dir)
    agent_test_dir = workspace / agent_test_root_rel(thread_id)
    if clean_state and not resume and agent_test_dir.exists():
        shutil.rmtree(agent_test_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = run_dir / "state_snapshot.json"
    if resume:
        if not snapshot_path.exists():
            raise FileNotFoundError(f"No resumable checkpoint exists for thread {thread_id}")
        checkpoint = read_json(snapshot_path)
        state = prepare_resumed_state(
            checkpoint,
            max_rounds=max_rounds,
            max_repair_calls=int(checkpoint.get("max_repair_llm_calls", 6) or 6),
            clarification_answer=clarification_answer,
        )
    else:
        state = {
        "task": task,
        "user_task": task,
        "original_task": task,
        "task_runtime_instructions": task_runtime_instructions,
        "workspace": str(workspace),
        "thread_id": thread_id,
        "max_rounds": max_rounds,
        "mode": "repair_existing" if repair_existing else "auto",
        "invariants": [
            "Do not use /tmp as the default output path.",
            "Do not weaken tests to hide implementation bugs; however, agent-generated tests may be corrected when a TestOracleReview finds they contradict the task contract or their own expected-value calculation.",
            "The final success decision must come from execution-based verification, not from the LLM.",
            "Every file modification must go through the agent's guarded tools.",
            "Prefer reading relevant files before writing changes.",
        ],
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "context_pack_path": str(run_dir / "context_pack.json"),
        "context_summary_path": str(run_dir / "context_summary.md"),
        "state_snapshot_path": str(run_dir / "state_snapshot.json"),
        "patches_dir": str(run_dir / "patches"),
        "final_path": str(run_dir / "final.json"),
        }
    graph = build_graph()
    try:
        result = graph.invoke(state, config={"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        tb = traceback.format_exc()
        crash = dict(state)
        crash.update(
            {
                "final_ok": False,
                "runtime_ok": False,
                "stopped_reason": "runtime_exception",
                "failure": {
                    "failure_type": "runtime_exception",
                    "message": str(exc),
                    "signature": exc.__class__.__name__,
                    "raw_excerpt": tb[-8000:],
                },
            }
        )
        final = {
            "ok": False,
            "runtime_ok": False,
            "stopped_reason": "runtime_exception",
            "task": task,
            "workspace": str(workspace),
            "thread_id": thread_id,
            "mode": crash.get("mode"),
            "failure": crash.get("failure"),
            "traceback_tail": tb[-8000:],
            "artifacts": {
                "trace": crash.get("trace_path"),
                "messages": crash.get("messages_path"),
                "state_snapshot": crash.get("state_snapshot_path"),
            },
        }
        write_json(run_dir / "final.json", final)
        write_json(run_dir / "state_snapshot.json", crash)
        raise
    final_path = Path(result.get("final_path") or run_dir / "final.json")
    final = read_json(final_path)
    final["final_path"] = str(final_path)
    human_report_path = run_dir / "final_report_human.md"
    write_human_report(human_report_path, final)
    final.setdefault("artifacts", {})["human_report"] = str(human_report_path)
    final["final_path"] = str(final_path)
    write_json(final_path, final)
    return final


def export_turn_audit(
    *,
    workspace: Path,
    thread_id: str,
    audit_dir: Path,
    include_workspace: bool,
) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    out = audit_dir / f"{thread_id}_audit.zip"
    return export_audit_bundle(workspace, thread_id, out, include_workspace=include_workspace)


def prompt_workspace() -> Path:
    raw = input("Workspace path: ").strip()
    if not raw:
        raise SystemExit("workspace is required")
    return Path(raw).expanduser().resolve()


def read_multiline(ui: TerminalUI, mode: str) -> str:
    ui.info(f"Multi-line input for {mode}. Finish with a single '.' line.")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def parse_interactive_input(ui: TerminalUI, current_mode: str, chat_title: str | None = None) -> dict[str, Any]:
    raw = ui.read_command(current_mode, chat_title=chat_title)
    if not raw:
        return {"action": "noop"}
    low = raw.lower()
    if low == "/":
        return {"action": "help"}
    if low in {"/quit", "/q", "quit"}:
        return {"action": "quit"}
    if low in {"/exit", "exit", "/back", "/default"}:
        return {"action": "exit_mode"}
    if low in {"/help", "help", "?"}:
        return {"action": "help"}
    if low == "/sessions":
        return {"action": "sessions"}
    if low == "/new":
        return {"action": "new_chat"}
    if low == "/continue":
        return {"action": "continue_chat", "choice": ""}
    if low.startswith("/continue "):
        _, choice = raw.split(" ", 1)
        return {"action": "continue_chat", "choice": choice.strip()}
    if low in {"/chat", "/ask"}:
        return {"action": "switch", "mode": "chat"}
    if low.startswith("/chat ") or low.startswith("/ask "):
        _, text = raw.split(" ", 1)
        return {"action": "chat", "text": text.strip()}
    if low in {"/code", "/agent"}:
        return {"action": "switch", "mode": "code"}
    if low.startswith("/code ") or low.startswith("/agent "):
        _, text = raw.split(" ", 1)
        return {"action": "code", "text": text.strip()}
    if low == "/repair":
        return {"action": "switch", "mode": "repair"}
    if low.startswith("/repair "):
        _, text = raw.split(" ", 1)
        return {"action": "repair", "text": text.strip()}
    if low == "/multi":
        text = read_multiline(ui, current_mode)
        if current_mode == "repair":
            return {"action": "repair", "text": text}
        if current_mode == "code":
            return {"action": "code", "text": text}
        return {"action": "chat", "text": text}
    if current_mode == "repair":
        return {"action": "repair", "text": raw}
    if current_mode == "code":
        return {"action": "code", "text": raw}
    return {"action": "chat", "text": raw}


def run_direct_chat(
    *,
    question: str,
    history: list[dict[str, str]],
    messages_path: Path,
    chat_store_dir: Path | None = None,
    chat_session_id: str | None = None,
) -> tuple[str, dict[str, int]]:
    before = summarize_token_usage(messages_path)
    client = OpenAICompatClient("configs/model.yaml", messages_path=messages_path)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise helpful assistant inside a terminal coding-agent UI. "
                "Answer normal conversation directly. Do not claim to inspect files unless the user ran an agent task.\n"
                + language_instruction_for_text(question, artifact="answer")
            ),
        },
        *history[-10:],
        {"role": "user", "content": question},
    ]
    answer = client.chat(messages, purpose="direct_chat")
    language_quality = response_language_quality(question, answer, artifact="answer")
    if not language_quality.get("ok"):
        answer = client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Rewrite the assistant answer to satisfy the user's language preference. "
                        "Do not add new facts. Preserve file paths, commands, API names, and code identifiers.\n"
                        + language_instruction_for_text(question, artifact="answer")
                    ),
                },
                {"role": "user", "content": f"User question:\n{question}\n\nPrevious answer:\n{answer}"},
            ],
            purpose="direct_chat_language_revision",
        )
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    if chat_store_dir and chat_session_id:
        append_chat_turn(chat_store_dir, chat_session_id, user_text=question, assistant_text=answer)
    after = summarize_token_usage(messages_path)
    return answer, token_usage_delta(after, before)


def main() -> None:
    args = parse_args()
    ui = TerminalUI()
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else prompt_workspace()
    client = OpenAICompatClient("configs/model.yaml")
    ui.banner(workspace, client.model, client.base_url)

    one_task = load_task_from_args(args)
    session_totals = empty_session_totals()
    audit_dir = default_audit_dir(workspace, args.audit_dir)
    agent_session_id = safe_id(args.thread_id or f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    chat_store_dir = Path(args.chat_store_dir).expanduser().resolve() if args.chat_store_dir else default_chat_store_dir().resolve()
    chat_store_dir.mkdir(parents=True, exist_ok=True)
    if args.continue_chat:
        choice = "" if args.continue_chat == "latest" else str(args.continue_chat)
        active_chat_meta = resolve_chat_session_choice(chat_store_dir, choice)
        if active_chat_meta is None:
            ui.warn("没有找到可恢复的聊天会话，已新建一个会话。")
            active_chat_meta = create_chat_session(chat_store_dir, workspace=workspace, model=client.model, base_url=client.base_url)
    else:
        active_chat_meta = create_chat_session(chat_store_dir, workspace=workspace, model=client.model, base_url=client.base_url)
    direct_messages_path = chat_session_paths(chat_store_dir, str(active_chat_meta["id"]))["llm_messages"]
    direct_history: list[dict[str, str]] = []
    direct_history.extend(load_chat_history(chat_store_dir, str(active_chat_meta["id"])))

    def activate_chat_session(meta: dict[str, Any]) -> None:
        nonlocal active_chat_meta, direct_messages_path, direct_history
        active_chat_meta = meta
        sid = str(meta["id"])
        direct_messages_path = chat_session_paths(chat_store_dir, sid)["llm_messages"]
        direct_history = load_chat_history(chat_store_dir, sid)
        ui.active_chat(meta)

    def new_blank_chat_session() -> None:
        meta = create_chat_session(chat_store_dir, workspace=workspace, model=client.model, base_url=client.base_url)
        activate_chat_session(meta)

    def run_one(
        task: str,
        turn_idx: int,
        *,
        interactive: bool,
        repair_existing: bool | None = None,
        task_workspace: Path | None = None,
        display_task: str | None = None,
        task_runtime_instructions: str = "",
    ) -> None:
        thread_id = make_thread_id(agent_session_id, turn_idx, interactive=interactive)
        repair_mode = args.repair_existing if repair_existing is None else repair_existing
        run_workspace = (task_workspace or workspace).expanduser().resolve()
        ui.user_message(display_task or task, title="User Task / 用户任务")
        if run_workspace != workspace:
            ui.info(f"本次任务 workspace 已切换为：{run_workspace}")
        ui.info(f"开始运行：thread_id={thread_id}")
        try:
            final = run_agent_task(
                workspace=run_workspace,
                task=task,
                task_runtime_instructions=task_runtime_instructions,
                thread_id=thread_id,
                max_rounds=args.max_rounds,
                repair_existing=repair_mode,
                clean_state=not args.no_clean_state,
            )
            clarification_rounds = 0
            while interactive and final.get("stopped_reason") == "clarification_required" and clarification_rounds < 3:
                clarification_rounds += 1
                questions = final.get("clarification_questions") or []
                for item in questions:
                    ui.warn(str(item.get("question") if isinstance(item, dict) else item))
                answer = input("clarification> ").strip()
                if not answer:
                    ui.warn("未提供补充信息，本次任务保持暂停，可稍后用同一 checkpoint 恢复。")
                    break
                final = run_agent_task(
                    workspace=run_workspace,
                    task=task,
                    task_runtime_instructions=task_runtime_instructions,
                    thread_id=thread_id,
                    max_rounds=args.max_rounds,
                    repair_existing=repair_mode,
                    clean_state=False,
                    resume=True,
                    clarification_answer=answer,
                )
        except Exception as exc:
            ui.error(f"Agent runtime exception: {exc}")
            if args.debug:
                traceback.print_exc()
            return
        turn_tokens = ((final.get("token_usage") or {}).get("totals") or {})
        add_token_totals(session_totals, turn_tokens)
        audit_zip = None
        if not args.no_audit:
            try:
                audit_zip = export_turn_audit(
                    workspace=run_workspace,
                    thread_id=thread_id,
                    audit_dir=audit_dir,
                    include_workspace=args.include_workspace_audit,
                )
            except Exception as exc:
                ui.warn(f"audit export failed: {exc}")
        ui.print_result(
            final,
            audit_zip=audit_zip,
            session_totals=session_totals,
            show_generated_tests=args.show_generated_tests,
            debug=args.debug,
        )

    def run_ask(question: str) -> None:
        nonlocal active_chat_meta
        ui.user_message(question, title="User Chat / 普通对话")
        try:
            if ui.console:
                with ui.console.status("[bold green]LLM is answering...[/bold green]", spinner="dots"):
                    answer, turn_tokens = run_direct_chat(
                        question=question,
                        history=direct_history,
                        messages_path=direct_messages_path,
                        chat_store_dir=chat_store_dir,
                        chat_session_id=str(active_chat_meta["id"]),
                    )
            else:
                answer, turn_tokens = run_direct_chat(
                    question=question,
                    history=direct_history,
                    messages_path=direct_messages_path,
                    chat_store_dir=chat_store_dir,
                    chat_session_id=str(active_chat_meta["id"]),
                )
        except Exception as exc:
            ui.error(f"Direct chat failed: {exc}")
            if args.debug:
                traceback.print_exc()
            return
        add_token_totals(session_totals, turn_tokens)
        latest_meta = load_chat_meta(chat_store_dir, str(active_chat_meta["id"]))
        if latest_meta:
            active_chat_meta = latest_meta
        ui.assistant_message(answer, title="Assistant / 普通回答")
        ui.print_chat_token_usage(turn_tokens, session_totals)

    if one_task:
        prepared = prepare_task_for_agent(
            one_task,
            base_workspace=workspace,
            mode="repair" if args.repair_existing else "code",
        )
        run_one(
            prepared.task,
            1,
            interactive=False,
            task_workspace=prepared.workspace,
            display_task=prepared.original_task,
            task_runtime_instructions=prepared.runtime_instructions,
        )
        return

    current_mode = "chat"
    ui.active_chat(active_chat_meta)
    ui.mode_help(current_mode)
    turn_idx = 1
    while True:
        chat_title = str(active_chat_meta.get("title") or "") if current_mode == "chat" else None
        command = parse_interactive_input(ui, current_mode, chat_title=chat_title)
        action = command.get("action")
        if action == "quit":
            ui.exit_summary(session_totals)
            break
        if action == "exit_mode":
            if current_mode == "chat":
                ui.info("已退出当前聊天上下文，进入空白新聊天。历史可用 /continue 恢复。")
                new_blank_chat_session()
            else:
                ui.info(f"已退出 {current_mode} 模式，回到普通聊天。")
                current_mode = "chat"
                ui.active_chat(active_chat_meta)
            ui.mode_help(current_mode)
            continue
        if action == "noop":
            continue
        if action == "help":
            ui.mode_help(current_mode)
            continue
        if action == "sessions":
            ui.print_chat_sessions(list_chat_sessions(chat_store_dir, limit=20))
            continue
        if action == "new_chat":
            new_blank_chat_session()
            current_mode = "chat"
            continue
        if action == "continue_chat":
            sessions = list_chat_sessions(chat_store_dir, limit=20)
            ui.print_chat_sessions(sessions)
            if not sessions:
                continue
            choice = str(command.get("choice") or "").strip()
            if not choice:
                choice = ui.read_command("continue", chat_title="输入编号或 id，回车默认 1").strip() or "1"
                if choice.lower() in {"/exit", "/quit", "quit", "exit"}:
                    continue
            meta = resolve_chat_session_choice(chat_store_dir, choice)
            if meta is None:
                ui.warn(f"没有找到聊天会话：{choice}")
                continue
            activate_chat_session(meta)
            current_mode = "chat"
            continue
        if action == "switch":
            current_mode = str(command.get("mode") or "chat")
            ui.mode_changed(current_mode)
            if current_mode == "chat":
                ui.active_chat(active_chat_meta)
            ui.mode_help(current_mode)
            continue

        text = str(command.get("text") or "").strip()
        if not text:
            continue
        if action == "chat":
            if should_auto_route_chat_to_code(text, workspace):
                prepared = prepare_task_for_agent(text, base_workspace=workspace, mode="code")
                ui.info("检测到真实目录路径和项目任务，已自动按 /code 执行。")
                run_one(
                    prepared.task,
                    turn_idx,
                    interactive=True,
                    repair_existing=False,
                    task_workspace=prepared.workspace,
                    display_task=prepared.original_task,
                    task_runtime_instructions=prepared.runtime_instructions,
                )
                turn_idx += 1
                continue
            run_ask(text)
            continue
        if action == "code":
            prepared = prepare_task_for_agent(text, base_workspace=workspace, mode="code")
            run_one(
                prepared.task,
                turn_idx,
                interactive=True,
                repair_existing=False,
                task_workspace=prepared.workspace,
                display_task=prepared.original_task,
                task_runtime_instructions=prepared.runtime_instructions,
            )
            turn_idx += 1
            continue
        if action == "repair":
            prepared = prepare_task_for_agent(text, base_workspace=workspace, mode="repair")
            run_one(
                prepared.task,
                turn_idx,
                interactive=True,
                repair_existing=True,
                task_workspace=prepared.workspace,
                display_task=prepared.original_task,
                task_runtime_instructions=prepared.runtime_instructions,
            )
            turn_idx += 1
            continue
        ui.warn(f"Unknown interactive action: {action}")
    return


if __name__ == "__main__":
    main()
