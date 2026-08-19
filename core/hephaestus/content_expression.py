"""Lightweight Wiki content expression formatter.

This is a post-processing helper for distilled Wiki content. It is rule-based,
does not call an LLM, and leaves raw capture untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ExpressionForm(Enum):
    MARKDOWN = "markdown"
    COMPARISON_TABLE = "comparison_table"
    MERMAID_FLOW = "mermaid_flow"
    CONFIG_BLOCK = "config_block"
    CHECKLIST = "checklist"


@dataclass(frozen=True)
class FormatSuggestion:
    form: ExpressionForm
    confidence: float
    reason: str


class ContentExpressionFormatter:
    """Heuristic Markdown expression enhancer for Wiki output."""

    MIN_CONFIDENCE = 0.45

    def detect_form(self, content: str) -> FormatSuggestion:
        text = content or ""
        lowered = text.lower()
        if "```" in text:
            return FormatSuggestion(ExpressionForm.MARKDOWN, 0.0, "contains code block")

        scores: list[FormatSuggestion] = []
        if re.search(r"(方案|选项|option|vs|对比|比较).*(优点|缺点|优势|劣势)", text, re.I | re.S):
            scores.append(FormatSuggestion(ExpressionForm.COMPARISON_TABLE, 0.82, "comparison"))
        if any(k in lowered for k in ("检查清单", "checklist", "核对", "确认")) and re.search(
            r"^\s*[-*]\s+(?!\[)", text, re.M
        ):
            scores.append(FormatSuggestion(ExpressionForm.CHECKLIST, 0.86, "checklist"))
        if re.search(r"(步骤|流程|先.+再|最后|if.+then|如果.+则)", text, re.I | re.S):
            scores.append(FormatSuggestion(ExpressionForm.MERMAID_FLOW, 0.72, "flow"))
        if re.search(r"^\s*[\w.-]+\s*[=:]\s*.+$", text, re.M) and any(
            k in lowered for k in ("配置", "config", "yaml", "json", "参数")
        ):
            scores.append(FormatSuggestion(ExpressionForm.CONFIG_BLOCK, 0.7, "config"))

        if not scores:
            return FormatSuggestion(ExpressionForm.MARKDOWN, 0.0, "no strong pattern")
        return max(scores, key=lambda item: item.confidence)

    def format_content(self, content: str, forced_form: ExpressionForm | None = None) -> str:
        if not content or "```" in content:
            return content
        suggestion = (
            FormatSuggestion(forced_form, 1.0, "forced") if forced_form else self.detect_form(content)
        )
        if suggestion.confidence < self.MIN_CONFIDENCE:
            return content
        if suggestion.form == ExpressionForm.COMPARISON_TABLE:
            return self._comparison_table(content)
        if suggestion.form == ExpressionForm.MERMAID_FLOW:
            return self._mermaid_flow(content)
        if suggestion.form == ExpressionForm.CONFIG_BLOCK:
            return self._config_block(content)
        if suggestion.form == ExpressionForm.CHECKLIST:
            return self._checklist(content)
        return content

    def _comparison_table(self, content: str) -> str:
        rows = []
        for line in [ln.strip() for ln in content.splitlines() if ln.strip()]:
            if re.search(r"方案|选项|option|vs|对比|比较", line, re.I):
                parts = re.split(r"\s{2,}|[：:]", line, maxsplit=1)
                option = parts[0].strip()
                detail = parts[1].strip() if len(parts) > 1 else line
                rows.append((option, detail))
        if not rows:
            rows = [("内容", content.strip())]
        table = ["| 选项 | 要点 |", "|---|---|"]
        table.extend(f"| {option} | {detail} |" for option, detail in rows)
        return "\n".join(table)

    def _mermaid_flow(self, content: str) -> str:
        parts = re.split(r"，|,|；|;|->|→|\n", content)
        steps = [p.strip(" -。.") for p in parts if p.strip()]
        if len(steps) < 2:
            steps = ["开始", content.strip(), "结束"]
        lines = ["```mermaid", "flowchart TD"]
        for idx, step in enumerate(steps):
            node = chr(ord("A") + idx)
            safe = step.replace('"', "'")
            lines.append(f'    {node}["{safe}"]')
            if idx > 0:
                prev = chr(ord("A") + idx - 1)
                lines.append(f"    {prev} --> {node}")
        lines.append("```")
        return "\n".join(lines)

    def _config_block(self, content: str) -> str:
        converted = []
        for line in content.splitlines():
            match = re.match(r"^\s*([\w.-]+)\s*=\s*(.+?)\s*$", line)
            if match:
                converted.append(f"{match.group(1)}: {match.group(2)}")
            elif ":" in line:
                converted.append(line.strip())
        if not converted:
            converted = [content.strip()]
        return "```yaml\n" + "\n".join(converted) + "\n```"

    def _checklist(self, content: str) -> str:
        lines = []
        for line in content.splitlines():
            match = re.match(r"^(\s*)[-*]\s+(?!\[)(.+)$", line)
            if match:
                lines.append(f"{match.group(1)}- [ ] {match.group(2).strip()}")
            else:
                lines.append(line)
        return "\n".join(lines)


def maybe_format_expression(content: str, config=None) -> str:
    """Apply expression formatting only when explicitly enabled in config."""
    if config is None:
        try:
            from core.config import get_config

            config = get_config()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            config = None
    enabled = bool(config and config.get("distill.auto_expression_formatting", True))
    if not enabled:
        return content
    return ContentExpressionFormatter().format_content(content)
