#!/usr/bin/env python3
"""Generate an event-bus producer/consumer map from static AST analysis.

Scans ``core/``, ``integrations/``, ``daemon/``, ``mnemos_cli.py`` and ``mnemos_daemon.py``
for event publishers and subscribers.  Producers are detected via:

* ``publish_event(...)`` helper
* ``_emit_event(...)`` (``PluggableModule`` helper)
* ``_publish_reminder_events(...)``
* ``<bus>.publish(...)``

Consumers are detected via:

* ``<bus>.subscribe(...)``
* ``PluggableModule.handle_event()`` branches

The registry constants ``EVENT_TYPES``, ``_PERSISTENT_EVENT_TYPES`` and
``_NO_PERSIST_EVENT_TYPES`` are extracted from ``core/mnemos_bus.py``.

Dynamic or runtime-constructed events that AST cannot see are supplied through
``scripts/event_bus_waivers.json``.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "event_bus_map.md"
DEFAULT_WAIVER_FILE = PROJECT_ROOT / "scripts" / "event_bus_waivers.json"

logger = logging.getLogger(__name__)

DEFAULT_SCAN_DIRS = ["core", "integrations", "daemon"]
DEFAULT_ROOT_FILES = ["mnemos_cli.py", "mnemos_daemon.py"]
EXCLUDE_DIR_NAMES = {
    ".venv",
    ".audit_venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".claude",
    "build",
    "dist",
}


@dataclass
class EventRef:
    """A single producer or consumer reference for an event type."""

    event_type: str
    file: Path
    line: int
    kind: str
    context: str
    snippet: str
    source: str = "code"


@dataclass
class EventBusMap:
    registered: List[str] = field(default_factory=list)
    persistent: Set[str] = field(default_factory=set)
    no_persist: Set[str] = field(default_factory=set)
    producers: List[EventRef] = field(default_factory=list)
    consumers: List[EventRef] = field(default_factory=list)

    def all_event_types(self) -> Set[str]:
        types: Set[str] = set(self.registered)
        for ref in self.producers:
            types.add(ref.event_type)
        for ref in self.consumers:
            types.add(ref.event_type)
        return types

    def producers_for(self, event_type: str) -> List[EventRef]:
        return [r for r in self.producers if r.event_type == event_type]

    def consumers_for(self, event_type: str) -> List[EventRef]:
        return [r for r in self.consumers if r.event_type == event_type]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _collect_files(root: Path) -> List[Path]:
    files: Set[Path] = set()
    for dir_name in DEFAULT_SCAN_DIRS:
        base = root / dir_name
        if not base.is_dir():
            continue
        for py_file in base.rglob("*.py"):
            if any(part in EXCLUDE_DIR_NAMES for part in py_file.parts):
                continue
            files.add(py_file.resolve())
    for file_name in DEFAULT_ROOT_FILES:
        py_file = root / file_name
        if py_file.is_file():
            files.add(py_file.resolve())
    return sorted(files)


def _str_literal(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _container_string_literals(node: ast.AST) -> List[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [
            value
            for elt in node.elts
            for value in [_str_literal(elt)]
            if value is not None
        ]
    return []


def _event_constructor_literal(node: ast.AST) -> Optional[str]:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_event_constructor = (
        isinstance(func, ast.Name)
        and func.id == "Event"
        or isinstance(func, ast.Attribute)
        and func.attr == "Event"
    )
    if not is_event_constructor:
        return None
    return _event_literal_from_call(node.args, node.keywords)


def _event_literal_from_call(
    args: List[ast.expr],
    keywords: List[ast.keyword],
    keyword_name: str = "event_type",
) -> Optional[str]:
    for kw in keywords:
        if kw.arg == keyword_name:
            return _str_literal(kw.value)
    if args:
        return _str_literal(args[0]) or _event_constructor_literal(args[0])
    return None


class _EventBusVisitor(ast.NodeVisitor):
    """Collect event producers and consumers from a single module."""

    def __init__(self, source: str, path: Path, root: Path) -> None:
        self.source = source
        self.path = path
        self.root = root
        self.producers: List[EventRef] = []
        self.consumers: List[EventRef] = []

        self._class_stack: List[str] = []
        self._func_stack: List[str] = []
        self._event_param: Optional[str] = None

        tree = ast.parse(source, filename=str(path))
        self._string_constants: Dict[str, str] = {}
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                value = _str_literal(statement.value)
                if value is not None:
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            self._string_constants[target.id] = value
        self.visit(tree)

    def _event_from_call(
        self, args: List[ast.expr], keywords: List[ast.keyword]
    ) -> Optional[str]:
        value = _event_literal_from_call(args, keywords)
        if value is not None:
            return value
        candidate: ast.AST | None = args[0] if args else None
        for keyword in keywords:
            if keyword.arg == "event_type":
                candidate = keyword.value
        if isinstance(candidate, ast.Name):
            return self._string_constants.get(candidate.id)
        return None

    def _context(self) -> str:
        parts = list(self._class_stack) + list(self._func_stack)
        return "::".join(parts) or "<module>"

    def _snippet(self, node: ast.AST) -> str:
        return (ast.get_source_segment(self.source, node) or "").strip()

    def _add(
        self,
        bucket: List[EventRef],
        event_type: str,
        kind: str,
        node: ast.AST,
    ) -> None:
        bucket.append(
            EventRef(
                event_type=event_type,
                file=self.path,
                line=getattr(node, "lineno", 1) or 1,
                kind=kind,
                context=self._context(),
                snippet=self._snippet(node),
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        old_event_param = self._event_param
        if node.name in {"handle_event", "trigger_event"} and len(node.args.args) >= 2:
            self._event_param = node.args.args[1].arg
        self.generic_visit(node)
        self._event_param = old_event_param
        self._func_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        # publish_event(event_type, agent, payload)
        if isinstance(func, ast.Name) and func.id == "publish_event":
            event_type = self._event_from_call(node.args, node.keywords)
            if event_type is not None:
                self._add(self.producers, event_type, "publish_event()", node)

        # _publish(event_type, agent, payload) — active_bridge helper
        elif isinstance(func, ast.Name) and func.id == "_publish":
            event_type = self._event_from_call(node.args, node.keywords)
            if event_type is not None:
                self._add(self.producers, event_type, "_publish()", node)

        # _emit_event(event_type, payload) — Name or Attribute
        elif (
            isinstance(func, ast.Name)
            and func.id == "_emit_event"
            or isinstance(func, ast.Attribute)
            and func.attr == "_emit_event"
        ):
            event_type = self._event_from_call(node.args, node.keywords)
            if event_type is not None:
                self._add(self.producers, event_type, "_emit_event()", node)

        # _publish_reminder_events(event_type, reminders)
        elif isinstance(func, ast.Attribute) and func.attr == "_publish_reminder_events":
            event_type = self._event_from_call(node.args, node.keywords)
            if event_type is not None:
                self._add(
                    self.producers,
                    event_type,
                    "_publish_reminder_events()",
                    node,
                )

        # <bus>.publish(event_type, ...)
        elif isinstance(func, ast.Attribute) and func.attr == "publish":
            event_type = self._event_from_call(node.args, node.keywords)
            if event_type is not None:
                self._add(self.producers, event_type, "EventBus.publish()", node)

        # <bus>.subscribe(event_type, handler)
        elif isinstance(func, ast.Attribute) and func.attr == "subscribe":
            event_type = self._event_from_call(node.args, node.keywords)
            if event_type is not None:
                self._add(self.consumers, event_type, "EventBus.subscribe()", node)

        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self._event_param is None:
            self.generic_visit(node)
            return

        # event_type == "..." or "..." == event_type
        if len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            for comparator in node.comparators:
                value = _str_literal(comparator)
                if value is not None and self._references_param(node.left):
                    self._add(
                        self.consumers,
                        value,
                        "PluggableModule.handle_event()",
                        node,
                    )
            # Chained comparisons: "..." == event_type == "..."
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                if self._references_param(node.comparators[0]):
                    self._add(
                        self.consumers,
                        node.left.value,
                        "PluggableModule.handle_event()",
                        node,
                    )

        # event_type in {...} / event_type not in (...)
        elif len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            if self._references_param(node.left):
                for value in _container_string_literals(node.comparators[0]):
                    self._add(
                        self.consumers,
                        value,
                        "PluggableModule.handle_event()",
                        node,
                    )

        self.generic_visit(node)

    def _references_param(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == self._event_param


def _extract_registry_sets(bus_path: Path) -> Dict[str, List[str]]:
    source = bus_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(bus_path))
    result: Dict[str, List[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in (
                "EVENT_TYPES",
                "_PERSISTENT_EVENT_TYPES",
                "_NO_PERSIST_EVENT_TYPES",
            ):
                continue
            if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                result[target.id] = [
                    value
                    for elt in node.value.elts
                    for value in [_str_literal(elt)]
                    if value is not None
                ]
    return result


def _load_waivers(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:  # pragma: no cover - exercised by tests
        logger.warning("Failed to parse waiver file %s: %s", path, exc)
        return {}
    return data.get("events", {}) if isinstance(data, dict) else {}


def _apply_waivers(
    bus_map: EventBusMap,
    waivers: Dict[str, Any],
    root: Path,
) -> None:
    for event_type, sections in waivers.items():
        for producer in sections.get("producers", []):
            bus_map.producers.append(
                EventRef(
                    event_type=event_type,
                    file=root / "scripts" / "event_bus_waivers.json",
                    line=0,
                    kind="waiver",
                    context=producer.get("source", "waiver"),
                    snippet=producer.get("note", ""),
                    source="waiver",
                )
            )
        for consumer in sections.get("consumers", []):
            bus_map.consumers.append(
                EventRef(
                    event_type=event_type,
                    file=root / consumer.get("file", "scripts/event_bus_waivers.json"),
                    line=consumer.get("line", 0),
                    kind="waiver",
                    context=consumer.get("context", "waiver"),
                    snippet=consumer.get("note", ""),
                    source="waiver",
                )
            )


def build_map(
    root: Path = PROJECT_ROOT,
    waiver_path: Path = DEFAULT_WAIVER_FILE,
) -> EventBusMap:
    """Build an event-bus map for the project rooted at ``root``.

    Scans ``core/``, ``integrations/``, ``mnemos_cli.py`` and
    ``mnemos_daemon.py`` for event producers and consumers, extracts the
    registry sets from ``core/mnemos_bus.py``, and merges any dynamic events
    described in ``waiver_path``.
    """
    root = root.resolve()
    if waiver_path == DEFAULT_WAIVER_FILE and root != PROJECT_ROOT:
        waiver_path = root / "scripts" / "event_bus_waivers.json"

    registry = _extract_registry_sets(root / "core" / "mnemos_bus.py")
    bus_map = EventBusMap(
        registered=registry.get("EVENT_TYPES", []),
        persistent=set(registry.get("_PERSISTENT_EVENT_TYPES", [])),
        no_persist=set(registry.get("_NO_PERSIST_EVENT_TYPES", [])),
    )

    for path in _collect_files(root):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            visitor = _EventBusVisitor(source, path, root)
        except SyntaxError:
            continue
        bus_map.producers.extend(visitor.producers)
        bus_map.consumers.extend(visitor.consumers)

    waivers = _load_waivers(waiver_path)
    _apply_waivers(bus_map, waivers, root)
    return bus_map


def _sort_refs(refs: Iterable[EventRef], root: Path) -> List[EventRef]:
    return sorted(
        refs,
        key=lambda r: (str(r.file), r.line, r.event_type, r.kind),
    )


def _ref_markdown(ref: EventRef, root: Path) -> str:
    if ref.source == "waiver":
        location = "`dynamic-waiver`"
    else:
        rel = _rel(ref.file, root)
        location = f"`{rel}:{ref.line}`" if ref.line else f"`{rel}`"
    if ref.line and ref.source != "waiver":
        location = f"`{rel}:{ref.line}`"
    context = f" `{ref.context}`" if ref.context else ""
    return f"- {location}{context} ({ref.kind})"


def _anchor(event_type: str) -> str:
    """Return a stable explicit Markdown anchor without collapsing underscores."""

    return event_type.replace(".", "-")


def _anomalies(bus_map: EventBusMap) -> Dict[str, List[str]]:
    anomalies: Dict[str, List[str]] = {
        "unregistered": [],
        "orphaned_registered": [],
        "no_producer": [],
        "no_consumer": [],
    }
    observed = bus_map.all_event_types()
    for event_type in sorted(observed - set(bus_map.registered)):
        anomalies["unregistered"].append(event_type)
    for event_type in bus_map.registered:
        has_producer = bool(bus_map.producers_for(event_type))
        has_consumer = bool(bus_map.consumers_for(event_type))
        if not has_producer and not has_consumer:
            anomalies["orphaned_registered"].append(event_type)
        elif not has_producer:
            anomalies["no_producer"].append(event_type)
        elif not has_consumer:
            anomalies["no_consumer"].append(event_type)
    return anomalies


def render_markdown(bus_map: EventBusMap, root: Path) -> str:
    lines: List[str] = []
    lines.append("# Mnemos Event Bus Map\n")
    lines.append(
        "_Generated from static AST analysis. Dynamic runtime-only events are "
        "annotated by the waiver file._\n"
    )

    # Summary
    anomalies = _anomalies(bus_map)
    lines.append("## Summary\n")
    lines.append(f"- **Registered event types**: {len(bus_map.registered)}")
    lines.append(f"- **Persistent event types**: {len(bus_map.persistent)}")
    lines.append(f"- **No-persist event types**: {len(bus_map.no_persist)}")
    lines.append(f"- **Producer references found**: {len(bus_map.producers)}")
    lines.append(f"- **Consumer references found**: {len(bus_map.consumers)}")
    lines.append(f"- **Unregistered observed events**: {len(anomalies['unregistered'])}")
    lines.append(
        f"- **Registered events with no producer/consumer**: "
        f"{len(anomalies['orphaned_registered'])}"
    )
    lines.append("")

    # Matrix
    lines.append("## Event Matrix\n")
    lines.append("| Event | Registered | Persistent | NoPersist | Producers | Consumers | Notes |")
    lines.append("|---|---|---|---|---|---|---|")
    for event_type in sorted(bus_map.all_event_types()):
        registered = "yes" if event_type in bus_map.registered else ""
        persistent = "yes" if event_type in bus_map.persistent else ""
        no_persist = "yes" if event_type in bus_map.no_persist else ""
        producer_count = len(bus_map.producers_for(event_type))
        consumer_count = len(bus_map.consumers_for(event_type))
        notes = []
        if event_type in anomalies["unregistered"]:
            notes.append("unregistered")
        if event_type in anomalies["orphaned_registered"]:
            notes.append("ORPHANED")
        elif event_type in anomalies["no_producer"]:
            notes.append("no producer")
        elif event_type in anomalies["no_consumer"]:
            notes.append("no consumer")
        note_str = ", ".join(notes)
        anchor = _anchor(event_type)
        lines.append(
            f"| [{event_type}](#{anchor}) | {registered} | {persistent} | {no_persist} | "
            f"{producer_count} | {consumer_count} | {note_str} |"
        )
    lines.append("")

    # Per-event detail
    for event_type in sorted(bus_map.all_event_types()):
        anchor = _anchor(event_type)
        lines.append(f"## {event_type} {{#{anchor}}}")
        producers = _sort_refs(bus_map.producers_for(event_type), root)
        consumers = _sort_refs(bus_map.consumers_for(event_type), root)
        if not producers and not consumers:
            lines.append("\n**ORPHANED** - no producers or consumers detected.\n")
            continue
        lines.append("\n### Producers\n")
        if producers:
            for ref in producers:
                lines.append(_ref_markdown(ref, root))
        else:
            lines.append("_No producers detected._")
        lines.append("\n### Consumers\n")
        if consumers:
            for ref in consumers:
                lines.append(_ref_markdown(ref, root))
        else:
            lines.append("_No consumers detected._")
        lines.append("")

    # Anomalies
    lines.append("## Anomalies\n")
    if anomalies["unregistered"]:
        lines.append("### Unregistered events observed in code\n")
        for event_type in anomalies["unregistered"]:
            lines.append(f"- `{event_type}`")
        lines.append("")
    if anomalies["orphaned_registered"]:
        lines.append("### Registered events with no producers or consumers\n")
        for event_type in anomalies["orphaned_registered"]:
            lines.append(f"- `{event_type}`")
        lines.append("")
    if anomalies["no_producer"]:
        lines.append("### Registered events with no detected producers\n")
        for event_type in anomalies["no_producer"]:
            lines.append(f"- `{event_type}`")
        lines.append("")
    if anomalies["no_consumer"]:
        lines.append("### Registered events with no detected consumers\n")
        for event_type in anomalies["no_consumer"]:
            lines.append(f"- `{event_type}`")
        lines.append("")
    if not any(anomalies.values()):
        lines.append("No anomalies detected.\n")

    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the event-bus producer/consumer map.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output Markdown path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--waiver",
        type=Path,
        default=DEFAULT_WAIVER_FILE,
        help=f"Waiver JSON path (default: {DEFAULT_WAIVER_FILE}).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help=f"Project root (default: {PROJECT_ROOT}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the output file is missing or stale.",
    )
    args = parser.parse_args(argv)

    bus_map = build_map(root=args.root, waiver_path=args.waiver)
    markdown = render_markdown(bus_map, args.root)
    if args.check:
        if not args.output.exists():
            print(f"Event bus map is missing: {args.output}")
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != markdown:
            print(
                "Event bus map is stale. "
                "Run: python3 scripts/generate_event_bus_map.py"
            )
            return 1
        print(f"Event bus map is up to date: {args.output}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"Wrote event bus map to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
