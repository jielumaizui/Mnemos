"""Help-text rendering for the top-level Mnemos parser."""

from __future__ import annotations

from argparse import ArgumentParser


def build_docstring(parser: ArgumentParser) -> str:
    """Generate a module docstring from top-level subparser choices."""

    lines = ["Mnemos - 命令行入口", "", "命令："]
    subparsers = parser._subparsers  # noqa: SLF001
    if subparsers is not None:
        actions = []
        for action in subparsers._group_actions:  # noqa: SLF001
            if hasattr(action, "_choices_actions"):
                actions.extend(action._choices_actions)  # noqa: SLF001
        for action in sorted(actions, key=lambda item: item.dest):
            desc = action.help or ""
            lines.append(f"    mnemos {action.dest:24s} {desc}")
    return "\n".join(lines)
