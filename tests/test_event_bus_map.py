"""Tests for the event-bus map generator and documentation."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = PROJECT_ROOT / "docs" / "event_bus_map.md"
GEN_PATH = PROJECT_ROOT / "scripts" / "generate_event_bus_map.py"
WAIVER_PATH = PROJECT_ROOT / "scripts" / "event_bus_waivers.json"
BUS_PATH = PROJECT_ROOT / "core" / "mnemos_bus.py"


def _load_generator():
    """Import ``scripts/generate_event_bus_map.py`` without touching ``sys.path``."""
    spec = importlib.util.spec_from_file_location("generate_event_bus_map", str(GEN_PATH))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["generate_event_bus_map"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _registered_event_types() -> List[str]:
    gen = _load_generator()
    registry = gen._extract_registry_sets(BUS_PATH)
    return cast(List[str], registry.get("EVENT_TYPES", []))


def _parse_doc_sections() -> Dict[str, Dict[str, bool]]:
    """Return a mapping of event type -> presence flags in ``docs/event_bus_map.md``."""
    text = DOC_PATH.read_text(encoding="utf-8")
    # Event headings look like: ## event.type {#event-type}
    pattern = re.compile(
        r"^## (?P<event>[\w.\-_]+)(?: \{#[\w\-]+\})?\n(?P<body>.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    sections: Dict[str, Dict[str, bool]] = {}
    for match in pattern.finditer(text):
        event = match.group("event").strip()
        body = match.group("body")
        sections[event] = {
            "has_producers": "### Producers" in body,
            "has_consumers": "### Consumers" in body,
            "is_orphaned": "**ORPHANED**" in body,
        }
    return sections


def test_generator_script_exists() -> None:
    assert GEN_PATH.is_file(), f"Generator script not found: {GEN_PATH}"


def test_waiver_file_is_valid_json() -> None:
    data = json.loads(WAIVER_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "events" in data


def _assert_is_string(obj: object, path: str) -> None:
    assert isinstance(obj, str), f"{path} must be a string, got {type(obj).__name__}"


def test_waiver_file_matches_schema() -> None:
    """Validate the structure of ``scripts/event_bus_waivers.json``."""
    data = json.loads(WAIVER_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "waiver root must be an object"
    assert set(data.keys()) == {"events"}, "waiver root must only contain an 'events' key"
    events = data["events"]
    assert isinstance(events, dict), "waiver 'events' must be an object"

    for event_name, event_def in events.items():
        _assert_is_string(event_name, "event name")
        assert isinstance(event_def, dict), f"event {event_name!r} must be an object"
        allowed_keys = {"producers", "consumers"}
        assert set(event_def.keys()) <= allowed_keys, (
            f"event {event_name!r} has unknown keys: " f"{set(event_def.keys()) - allowed_keys}"
        )
        for role in ("producers", "consumers"):
            refs = event_def.get(role, [])
            assert isinstance(refs, list), f"event {event_name!r} {role} must be a list"
            for idx, ref in enumerate(refs):
                prefix = f"event {event_name!r} {role}[{idx}]"
                assert isinstance(ref, dict), f"{prefix} must be an object"
                assert "source" in ref, f"{prefix} missing required 'source'"
                assert "note" in ref, f"{prefix} missing required 'note'"
                _assert_is_string(ref["source"], f"{prefix}.source")
                _assert_is_string(ref["note"], f"{prefix}.note")
                assert set(ref.keys()) == {
                    "source",
                    "note",
                }, f"{prefix} has unknown keys: {set(ref.keys()) - {'source', 'note'}}"


def test_documentation_exists() -> None:
    assert DOC_PATH.is_file(), f"Event bus map not found: {DOC_PATH}"


def test_every_registered_event_has_doc_section() -> None:
    registered = _registered_event_types()
    assert registered, "No events found in core/mnemos_bus.py EVENT_TYPES"

    sections = _parse_doc_sections()
    missing = [event for event in registered if event not in sections]
    assert not missing, f"Registered events missing from {DOC_PATH}: {missing}"

    incomplete: List[str] = []
    for event in registered:
        info = sections[event]
        # An ORPHANED event is acceptable; otherwise it must have at least a
        # Producers and Consumers subsection (the subsection may be empty).
        if not info["is_orphaned"] and (not info["has_producers"] or not info["has_consumers"]):
            incomplete.append(event)
    assert not incomplete, f"Registered events without Producers/Consumers sections: {incomplete}"


@pytest.mark.skipif(not GEN_PATH.exists(), reason="generator script not present")
def test_doc_is_up_to_date() -> None:
    """Regenerate the map to a temp path and assert it matches the committed doc."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output = Path(tmpdir) / "event_bus_map.md"
        result = subprocess.run(
            [sys.executable, str(GEN_PATH), "--output", str(output)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        assert (
            result.returncode == 0
        ), f"Generator failed:\nstdout={result.stdout}\nstderr={result.stderr}"

        generated = output.read_text(encoding="utf-8")
        committed = DOC_PATH.read_text(encoding="utf-8")
        assert generated == committed, (
            "docs/event_bus_map.md is out of date. " "Run: python scripts/generate_event_bus_map.py"
        )


def test_check_mode_accepts_current_doc() -> None:
    """``--check`` enforces that the committed map matches current code."""
    result = subprocess.run(
        [sys.executable, str(GEN_PATH), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Check failed:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_build_map_returns_structured_result() -> None:
    """``build_map()`` returns a populated ``EventBusMap``."""
    gen = _load_generator()
    bus_map = gen.build_map()
    assert bus_map.registered
    assert isinstance(bus_map.persistent, set)
    assert isinstance(bus_map.no_persist, set)
    assert bus_map.producers or bus_map.consumers
    # Every registered event appears in all_event_types().
    assert set(bus_map.registered) <= bus_map.all_event_types()


def test_visitor_detects_known_producers_and_consumers() -> None:
    """The AST visitor finds at least one known producer and consumer."""
    gen = _load_generator()
    bus_map = gen.build_map()
    producer_kinds = {ref.kind for ref in bus_map.producers}
    consumer_kinds = {ref.kind for ref in bus_map.consumers}
    assert "EventBus.publish()" in producer_kinds
    assert "EventBus.subscribe()" in consumer_kinds
    wiki_consumers = bus_map.consumers_for("wiki_page_updated")
    assert len(wiki_consumers) >= 6
    paths = {ref.file.as_posix() for ref in wiki_consumers}
    assert any(path.endswith("core/cognitive_graph/updater.py") for path in paths)
    assert sum(
        ref.file.as_posix().endswith("daemon/wiki_projection_handlers.py")
        for ref in wiki_consumers
    ) >= 5


def test_malformed_waiver_file_is_handled() -> None:
    """A malformed waiver file returns empty waivers without crashing."""
    gen = _load_generator()
    with tempfile.TemporaryDirectory() as tmpdir:
        bad_waiver = Path(tmpdir) / "waivers.json"
        bad_waiver.write_text("not json", encoding="utf-8")
        bus_map = gen.build_map(waiver_path=bad_waiver)
        # The map should still be usable; no waiver refs were added.
        assert all(ref.source != "waiver" for ref in bus_map.producers)
        assert all(ref.source != "waiver" for ref in bus_map.consumers)


def test_waiver_entries_merge_into_bus_map() -> None:
    """Valid waiver entries appear as additional producer/consumer refs."""
    gen = _load_generator()
    with tempfile.TemporaryDirectory() as tmpdir:
        waiver = Path(tmpdir) / "waivers.json"
        waiver.write_text(
            json.dumps(
                {
                    "events": {
                        "waiver.test.event": {
                            "producers": [{"source": "test", "note": "producer note"}],
                            "consumers": [{"source": "test", "note": "consumer note"}],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        bus_map = gen.build_map(waiver_path=waiver)
        producers = bus_map.producers_for("waiver.test.event")
        consumers = bus_map.consumers_for("waiver.test.event")
        assert producers and all(ref.source == "waiver" for ref in producers)
        assert consumers and all(ref.source == "waiver" for ref in consumers)
