"""Shared helpers for Mnemos active agent integration.

This module owns the small amount of cross-agent glue needed to expose the
current Mnemos MCP server and to create a preflight context that host agents can
consume at session start. Passive capture still remains the fidelity fallback.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


SERVER_NAME = "mnemos"
POLICY_MARKER = "MNEMOS_ACTIVE_POLICY"


def mnemos_root() -> Path:
    return Path(__file__).resolve().parents[1]


def mnemos_cli_path() -> Path:
    return mnemos_root() / "mnemos_cli.py"


def mcp_server_spec(python_cmd: str | None = None, *, claude: bool = False) -> Dict[str, Any]:
    """Return the stdio MCP server spec used by supported agents."""
    spec: Dict[str, Any] = {
        "command": python_cmd or sys.executable,
        "args": [str(mnemos_cli_path()), "mcp", "serve"],
    }
    if claude:
        spec = {"type": "stdio", **spec, "env": {}}
    return spec


def opencode_mcp_server_spec(python_cmd: str | None = None) -> Dict[str, Any]:
    """Return the current OpenCode local MCP server spec."""
    return {
        "type": "local",
        "command": [
            python_cmd or sys.executable,
            str(mnemos_cli_path()),
            "mcp",
            "serve",
        ],
        "enabled": True,
        "timeout": 10000,
    }


def codex_mcp_table(python_cmd: str | None = None) -> str:
    spec = mcp_server_spec(python_cmd)
    args = ", ".join(json.dumps(a, ensure_ascii=False) for a in spec["args"])
    return (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = {json.dumps(spec['command'], ensure_ascii=False)}\n"
        f"args = [{args}]\n"
    )


def write_text_if_changed(path: Path, text: str, *, backup: bool = True) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return True
    if backup and old is not None:
        backup_path = path.with_name(path.name + ".mnemos.bak")
        if not backup_path.exists():
            backup_path.write_text(old, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return True


def load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _strip_jsonc_comments(text: str) -> str:
    """Best-effort JSONC cleaner for user config files.

    OpenCode accepts JSONC. Python's stdlib does not, so we preserve data by
    stripping comments/trailing commas and writing a clean JSON file with a
    backup when comments were present.
    """
    out = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    return re.sub(r",\s*([}\]])", r"\1", cleaned)


def load_jsonc_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except Exception:
        try:
            data = json.loads(_strip_jsonc_comments(raw))
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def upsert_json_mcp_server(path: Path, *, top_key: str = "mcpServers",
                           claude: bool = False) -> bool:
    data = load_json_file(path)
    servers = data.get(top_key)
    if not isinstance(servers, dict):
        servers = {}
        data[top_key] = servers
    servers[SERVER_NAME] = mcp_server_spec(claude=claude)
    return write_text_if_changed(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def json_mcp_configured(path: Path, *, top_key: str = "mcpServers") -> bool:
    data = load_json_file(path)
    servers = data.get(top_key)
    if not isinstance(servers, dict):
        return False
    spec = servers.get(SERVER_NAME)
    if not isinstance(spec, dict):
        return False
    args = spec.get("args") or []
    return spec.get("command") and str(mnemos_cli_path()) in [str(a) for a in args]


def upsert_opencode_config(path: Path, *, include_mcp: bool = True,
                           include_policy: bool = True) -> bool:
    """Write current OpenCode config fields while preserving unrelated keys."""
    data = load_jsonc_file(path)
    if include_mcp:
        mcp = data.get("mcp")
        if not isinstance(mcp, dict):
            mcp = {}
            data["mcp"] = mcp
        mcp[SERVER_NAME] = opencode_mcp_server_spec()
    if include_policy:
        policy = str(write_active_policy_file())
        instructions = data.get("instructions")
        if isinstance(instructions, str):
            instructions = [instructions]
        elif not isinstance(instructions, list):
            instructions = []
        if policy not in [str(x) for x in instructions]:
            instructions.append(policy)
        data["instructions"] = instructions
    return write_text_if_changed(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def opencode_mcp_configured(path: Path) -> bool:
    data = load_jsonc_file(path)
    spec = data.get("mcp", {}).get(SERVER_NAME)
    if not isinstance(spec, dict):
        return False
    command = spec.get("command") or []
    if isinstance(command, str):
        command = [command]
    return str(mnemos_cli_path()) in [str(a) for a in command]


def opencode_policy_configured(path: Path) -> bool:
    data = load_jsonc_file(path)
    instructions = data.get("instructions")
    if isinstance(instructions, str):
        instructions = [instructions]
    if not isinstance(instructions, list):
        return False
    return str(active_policy_path()) in [str(x) for x in instructions]


def upsert_openclaw_mcp_server(path: Path) -> bool:
    data = load_json_file(path)
    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        mcp = {}
        data["mcp"] = mcp
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        servers = {}
        mcp["servers"] = servers
    servers[SERVER_NAME] = mcp_server_spec()
    return write_text_if_changed(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def openclaw_mcp_configured(path: Path) -> bool:
    data = load_json_file(path)
    try:
        spec = data["mcp"]["servers"][SERVER_NAME]
    except Exception:
        return False
    args = spec.get("args") or []
    return spec.get("command") and str(mnemos_cli_path()) in [str(a) for a in args]


def upsert_codex_mcp_server(path: Path) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(r"(?ms)^\[mcp_servers\.mnemos\]\n.*?(?=^\[|\Z)")
    stripped = pattern.sub("", old).rstrip()
    table = codex_mcp_table()
    new = (stripped + "\n\n" if stripped else "") + table
    return write_text_if_changed(path, new)


def codex_mcp_configured(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    # Windows 兼容性：路径比较统一使用正斜杠
    cli_path = mnemos_cli_path().as_posix()
    return (
        "[mcp_servers.mnemos]" in text
        and cli_path in text
        and "mcp" in text
        and "serve" in text
    )


def upsert_marked_block(path: Path, content: str, *, marker: str = POLICY_MARKER) -> bool:
    start = f"<!-- BEGIN {marker} -->"
    end = f"<!-- END {marker} -->"
    block = f"{start}\n{content.rstrip()}\n{end}"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(
        rf"(?ms)<!-- BEGIN {re.escape(marker)} -->.*?<!-- END {re.escape(marker)} -->"
    )
    if pattern.search(old):
        new = pattern.sub(block, old)
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + block + "\n"
    return write_text_if_changed(path, new)


def marked_block_installed(path: Path, *, marker: str = POLICY_MARKER) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return f"<!-- BEGIN {marker} -->" in text and f"<!-- END {marker} -->" in text


def upsert_yaml_mcp_server(path: Path, *, top_key: str = "mcp_servers") -> bool:
    try:
        import yaml
    except Exception:
        return False
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
    servers = data.get(top_key)
    if not isinstance(servers, dict):
        servers = {}
        data[top_key] = servers
    servers[SERVER_NAME] = mcp_server_spec()
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return write_text_if_changed(path, text)


def yaml_mcp_configured(path: Path, *, top_key: str = "mcp_servers") -> bool:
    try:
        import yaml
    except Exception:
        return False
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        spec = data.get(top_key, {}).get(SERVER_NAME)
    except Exception:
        return False
    if not isinstance(spec, dict):
        return False
    args = spec.get("args") or []
    return spec.get("command") and str(mnemos_cli_path()) in [str(a) for a in args]


def upsert_kimi_hooks(config_path: Path, wrapper_path: Path) -> bool:
    """Install Kimi hook commands without requiring tomli_w.

    Kimi stores hooks in the main TOML config, while MCP servers live in
    ~/.kimi/mcp.json. We keep this writer deliberately narrow to avoid
    re-serializing user credentials or unrelated settings.
    """
    start = f'    {{ command = "python3 {wrapper_path} --session-start", event = "SessionStart" }},'
    end = f'    {{ command = "python3 {wrapper_path} --session-end", event = "SessionEnd" }},'
    if config_path.exists():
        text = config_path.read_text(encoding="utf-8")
    else:
        text = ""

    if "hooks" not in text:
        block = "hooks = [\n" + start + "\n" + end + "\n]\n"
        return write_text_if_changed(config_path, (text.rstrip() + "\n\n" if text.strip() else "") + block)

    lines = text.splitlines()
    out: List[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if not replaced and re.match(r"^\s*hooks\s*=\s*\[", line):
            out.append(line)
            i += 1
            while i < len(lines):
                current = lines[i]
                if str(wrapper_path) not in current and "mnemos_wrapper.py" not in current:
                    if current.strip() == "]":
                        out.append(start)
                        out.append(end)
                        out.append(current)
                        i += 1
                        break
                    out.append(current)
                i += 1
            replaced = True
            continue
        out.append(line)
        i += 1

    if not replaced:
        out.append("")
        out.extend(["hooks = [", start, end, "]"])
    return write_text_if_changed(config_path, "\n".join(out).rstrip() + "\n")


def kimi_hooks_configured(config_path: Path, wrapper_path: Path) -> bool:
    if not config_path.exists() or not wrapper_path.exists():
        return False
    text = config_path.read_text(encoding="utf-8")
    return (
        f"python3 {wrapper_path} --session-start" in text
        and f"python3 {wrapper_path} --session-end" in text
    )


def active_policy_path() -> Path:
    return Path.home() / ".mnemos" / "active_policy" / "MNEMOS_ACTIVE.md"


def active_policy_text(agent: str = "") -> str:
    agent_line = f" for {agent}" if agent else ""
    return f"""# Mnemos Active Policy{agent_line}

You have access to Mnemos as the user's long-term AI memory and knowledge system.

Before meaningful planning or answering:
- Use Mnemos startup context if it is provided.
- For coding, debugging, architecture, review, writing, or project decisions, call the Mnemos MCP tool `preflight_inject` or the server-prefixed equivalent.
- If the task may depend on earlier conversations, prior decisions, preferences, or project history, call `session_search`.
- If the task may depend on durable knowledge, standards, lessons, or Obsidian notes, call `context_aware_search` or `wiki_search`.

During the task:
- Prefer specific retrieved evidence over vague memory.
- Keep Mnemos context concise; do not paste large dumps unless the user asks.
- When retrieved knowledge conflicts with the user's latest explicit instruction, explain the conflict and follow the latest instruction unless it is unsafe.

Before finalizing high-impact output:
- Call `guard_check` when the answer changes code, architecture, deployment, data capture, security, user workflows, or system behavior.

At session end or after valuable decisions:
- Use `capture_session`, `capture_turn`, `end_session`, or the installed hook/wrapper when available.
- Passive capture remains the fidelity fallback, but do not rely on it as the only path when an explicit Mnemos tool is available.

If a Mnemos MCP tool is unavailable:
- Say so briefly when it matters, then proceed with the local startup context and the user's latest instruction.
"""


def write_active_policy_file() -> Path:
    path = active_policy_path()
    write_text_if_changed(path, active_policy_text(), backup=False)
    return path


def install_agent_policy(agent: str) -> bool:
    policy = write_active_policy_file()
    agent = agent.lower()
    if agent == "claude":
        return upsert_marked_block(Path.home() / ".claude" / "CLAUDE.md", active_policy_text(agent))
    if agent == "codex":
        return upsert_marked_block(Path.home() / ".codex" / "AGENTS.md", active_policy_text(agent))
    if agent == "opencode":
        return upsert_opencode_config(opencode_config_path(), include_mcp=False, include_policy=True)
    if agent == "kimi":
        return upsert_marked_block(Path.home() / ".kimi" / "MNEMOS_ACTIVE.md", active_policy_text(agent))
    if agent == "hermes":
        return upsert_marked_block(Path.home() / ".hermes" / "MNEMOS_ACTIVE.md", active_policy_text(agent))
    if agent == "openclaw":
        return upsert_marked_block(Path.home() / ".openclaw" / "MNEMOS_ACTIVE.md", active_policy_text(agent))
    return policy.exists()


def is_agent_policy_installed(agent: str) -> bool:
    agent = agent.lower()
    if not active_policy_path().exists():
        return False
    if agent == "claude":
        return marked_block_installed(Path.home() / ".claude" / "CLAUDE.md")
    if agent == "codex":
        return marked_block_installed(Path.home() / ".codex" / "AGENTS.md")
    if agent == "opencode":
        return opencode_policy_configured(opencode_config_path())
    if agent == "kimi":
        return marked_block_installed(Path.home() / ".kimi" / "MNEMOS_ACTIVE.md")
    if agent == "hermes":
        return marked_block_installed(Path.home() / ".hermes" / "MNEMOS_ACTIVE.md")
    if agent == "openclaw":
        return marked_block_installed(Path.home() / ".openclaw" / "MNEMOS_ACTIVE.md")
    return True


def opencode_config_path() -> Path:
    return Path.home() / ".config" / "opencode" / "opencode.json"


def active_context_path(agent: str) -> Path:
    return Path.home() / ".mnemos" / "active_context" / agent / "latest.md"


def render_active_context(agent: str, working_dir: str = "", user_message: str = "") -> str:
    timeout_sec = float(os.environ.get("MNEMOS_PREFLIGHT_TIMEOUT_SEC", "5"))
    try:
        kia_context = _run_preflight_with_timeout(
            agent,
            working_dir or os.getcwd(),
            user_message or "",
            timeout_sec,
        )
    except TimeoutError:
        kia_context = (
            f"Mnemos preflight exceeded {timeout_sec:g}s and was skipped for startup responsiveness.\n\n"
            "Use the Mnemos MCP tools when relevant:\n"
            "- preflight_inject for task-specific knowledge loading\n"
            "- context_aware_search or wiki_search for knowledge lookup\n"
            "- guard_check before finalizing high-impact answers\n"
        )
    except Exception as exc:
        kia_context = f"Mnemos preflight failed: {exc}"
    now = datetime.now().isoformat(timespec="seconds")
    return "\n".join([
        "# Mnemos Active Context",
        "",
        f"- Agent: {agent}",
        f"- Working directory: {working_dir or os.getcwd()}",
        f"- Generated at: {now}",
        "",
        "## Use This",
        "",
        "Use the following Mnemos knowledge before planning or answering. "
        "If it conflicts with the current task, explain the conflict and prefer the user's latest explicit instruction.",
        "",
        "## Active Policy",
        "",
        active_policy_text(agent).strip(),
        "",
        "## Context",
        "",
        kia_context.strip() or "No relevant Mnemos context was found for this session.",
        "",
    ])


def _run_preflight_with_timeout(agent: str, working_dir: str, user_message: str, timeout_sec: float) -> str:
    if timeout_sec <= 0 or not hasattr(signal, "SIGALRM"):
        with redirect_stdout(StringIO()):
            return _build_lightweight_preflight(agent, working_dir, user_message)

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum, _frame):
        raise TimeoutError("Mnemos preflight timeout")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        with redirect_stdout(StringIO()):
            return _build_lightweight_preflight(agent, working_dir, user_message)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _build_lightweight_preflight(agent: str, working_dir: str, user_message: str) -> str:
    """Build startup context without Memos API reads.

    Session-start needs to feel instant. Deeper historical recall remains
    available through MCP tools such as session_search and context_aware_search.
    """
    parts: List[str] = []
    task_type = _default_task_type(agent)
    query = " ".join(p for p in [user_message, Path(working_dir).name] if p).strip()

    try:
        from core.kia.kairos import TimeWindow, TimeWindowType
        from core.kia.prophasis import PreFlightInjector

        knowledge = PreFlightInjector().inject(
            task_type,
            "",
            TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0),
            query,
        )
        if knowledge:
            parts.append(_format_loaded_knowledge(knowledge))
    except Exception as exc:
        parts.append(f"## KIA Checklist\n\nMnemos checklist loading failed: {exc}")

    if query:
        try:
            from core.config import get_config
            from integrations.oracle import WikiReader

            searcher = WikiReader(str(get_config().wiki_dir))
            results = searcher.search(query, limit=5)
            if results:
                lines = ["## Related Wiki Knowledge", ""]
                for result in results[:5]:
                    title = result.get("title", "") or result.get("page_id", "")
                    page = result.get("page_id", "")
                    score = result.get("relevance_score", result.get("score", 0))
                    snippet = (result.get("snippet", "") or result.get("summary", "") or "").replace("\n", " ").strip()
                    lines.append(f"- {title} ({page}, score={score:.2f}): {snippet[:220]}")
                parts.append("\n".join(lines))
        except Exception as exc:
            parts.append(f"## Related Wiki Knowledge\n\nContext search failed: {exc}")

    try:
        from core.kia.preflight import get_persona_behavior_prompt

        persona = get_persona_behavior_prompt(agent)
        if persona:
            parts.append(persona)
    except Exception:
        pass

    parts.append(
        "## Active Tooling\n\n"
        "For prior conversation recall, call `session_search`. "
        "For deeper knowledge lookup, call `context_aware_search` or `wiki_search`. "
        "Before high-impact final answers, call `guard_check`."
    )
    return "\n\n".join(p for p in parts if p.strip())


def _default_task_type(agent: str) -> str:
    if agent in {"claude", "codex", "opencode"}:
        return "coding"
    return "general"


def _format_loaded_knowledge(knowledge: Any) -> str:
    lines = [
        "## KIA Checklist",
        "",
        f"- Task type: {knowledge.task_type}",
        f"- Loaded version: {knowledge.version}",
    ]
    if getattr(knowledge, "is_compact", False):
        lines.append(f"- Compact view: showing {len(knowledge.checklist)}/{knowledge.total_items} items")
    if getattr(knowledge, "lessons_summary", ""):
        lines.extend(["", "### Lessons", "", str(knowledge.lessons_summary).strip()])
    if getattr(knowledge, "checklist", None):
        lines.extend(["", "### Checklist", ""])
        for item in knowledge.checklist[:10]:
            severity = getattr(item, "severity", "medium")
            text = getattr(item, "item", str(item))
            lines.append(f"- [{severity}] {text}")
    return "\n".join(lines)


def write_active_context(agent: str, working_dir: str = "", user_message: str = "") -> Tuple[Path, str]:
    text = render_active_context(agent, working_dir, user_message)
    path = active_context_path(agent)
    write_text_if_changed(path, text, backup=False)
    return path, text


def generated_wrapper(agent: str) -> str:
    root = mnemos_root()
    return f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mnemos active bridge wrapper for {agent}."""

import sys

sys.path.insert(0, {str(root)!r})

from integrations.active_bridge import main


if __name__ == "__main__":
    main({agent!r})
'''


def wrapper_uses_active_bridge(path: Path) -> bool:
    return path.exists() and "integrations.active_bridge" in path.read_text(encoding="utf-8", errors="ignore")
