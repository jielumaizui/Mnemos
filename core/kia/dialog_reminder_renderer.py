"""Rendering boundary for dialog and Wiki reminder presentations."""

from __future__ import annotations

from typing import List, Protocol, Sequence


class ReminderEntryView(Protocol):
    """Minimal presentation contract needed by reminder renderers."""

    reminder_id: str
    issue_id: str
    severity: str
    content: str
    choices: List[str]


class ReminderRenderer:
    """提醒内容渲染器

    生成带 `[选择：xxx]` 交互选项的对话内容，
    以及 Wiki 页面横幅内容。
    """

    @staticmethod
    def render_dialog(entry: ReminderEntryView) -> str:
        """
        渲染带交互选项的对话推送内容。

        示例输出：
            <wiki-context type="reminder" severity="high" issue_id="rem-abc">
            📅 知识提醒：「Redis 连接池配置」

            此知识基于 Redis 6.0，你最近讨论了 Redis 7.2，建议确认是否仍有效。

            [选择：已更新] [选择：仍有效] [选择：忽略]
            </wiki-context>
        """
        severity_emoji = {
            "critical": "⚠️",
            "high": "📅",
            "medium": "📋",
            "low": "💡",
        }.get(entry.severity, "📋")

        lines = [
            f'<wiki-context type="reminder" severity="{entry.severity}" issue_id="{entry.reminder_id}">',  # noqa: E501
            f"{severity_emoji} {entry.content}",
            "",
        ]
        if entry.choices:
            choice_str = " ".join(f"[选择：{c}]" for c in entry.choices)
            lines.append(choice_str)
        lines.append("</wiki-context>")
        return "\n".join(lines)

    @staticmethod
    def render_banner(entry: ReminderEntryView) -> List[str]:
        """
        渲染 Wiki 页面横幅内容行。

        使用 Markdown 任务列表作为交互元素，用户在 Obsidian 中打勾后，
        Mnemos 定期扫描并执行对应操作。

        示例输出：
            > ⚠️ **知识提醒**（自动生成，处理后可删除）
            >
            > Redis 连接池配置 已 180 天未更新，请确认是否仍有效。
            >
            > 请选择一项（在对应方框内打勾）：
            >
            > - [ ] 已更新
            > - [ ] 仍有效
            > - [ ] 忽略
        """
        severity_emoji = {
            "critical": "⚠️",
            "high": "📅",
            "medium": "📋",
            "low": "💡",
        }.get(entry.severity, "📋")

        lines = [
            f"> {severity_emoji} **知识提醒**（自动生成，处理后可删除）",
            ">",
            f"> {entry.content}",
            ">",
            "> 请选择一项（在对应方框内打勾）：",
            ">",
        ]
        if entry.choices:
            for choice in entry.choices:
                lines.append(f"> - [ ] {choice}")
        else:
            lines.append("> - [ ] 已处理")
        return lines

    @staticmethod
    def render_aggregated_dialog(
        page_title: str,
        entries: Sequence[ReminderEntryView],
    ) -> str:
        """
        渲染聚合提醒（同一页面多个问题合并）。

        示例：
            <wiki-context type="reminder" severity="medium">
            📋 [[Docker Compose]] 存在 3 个优化建议：
            - 孤立页面（无关联）
            - 内容过短（80 字符）

            [查看详情] [忽略全部]
            </wiki-context>
        """
        lines = [
            '<wiki-context type="reminder" severity="medium">',
            f"📋 [[{page_title}]] 存在 {len(entries)} 个优化建议：",
            "",
        ]
        for e in entries:
            desc = e.content.strip().split("\n")[0] if e.content else e.issue_id
            lines.append(f"- {desc}")
        lines.extend(
            [
                "",
                "[选择：查看详情] [选择：忽略全部]",
                "</wiki-context>",
            ]
        )
        return "\n".join(lines)
