import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.reflection import consumers as consumers_module
from core.reflection.consumers import (
    CompositeConsumer,
    KIAExperienceConsumer,
    PersonaSignalConsumer,
)
from core.reflection.models import (
    ReflectionRecord,
    ReflectionTrigger,
)


def _make_insight(dims, confidence=0.8):
    return SimpleNamespace(
        summary="summary",
        key_points=["kp"],
        dimensions_involved=dims,
        confidence=confidence,
    )


def _make_record(dims=None, insight_confidence=0.8):
    dims = dims or ["attention", "growth"]
    return ReflectionRecord(
        id="r1",
        created_at=datetime.now(),
        trigger=ReflectionTrigger.MAJOR_DECISION,
        mirror_dimensions=dims,
        insight=_make_insight(dims, insight_confidence),
    )


@pytest.fixture
def patched_consumers_config(monkeypatch, patched_get_config):
    """Patch get_config inside core.reflection.consumers to use the fake config."""
    monkeypatch.setattr(consumers_module, "get_config", lambda: patched_get_config)
    return patched_get_config


def test_persona_signal_consumer_fallback_jsonl(tmp_path, patched_consumers_config):
    consumer = PersonaSignalConsumer(persona_store=None)
    records = [_make_record() for _ in range(5)]

    for record in records:
        consumer.on_insight_generated(record)

    fallback = Path(patched_consumers_config.database_dir) / "reflection_signals.jsonl"
    assert fallback.exists()

    lines = fallback.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 10
    data = [json.loads(line) for line in lines]
    dimensions = [d["dimension"] for d in data]
    assert dimensions.count("reflection_interest") == 10
    assert any(d["value"] == "attention" for d in data)
    assert any(d["value"] == "growth" for d in data)


def test_kia_experience_consumer_fallback_jsonl(tmp_path, patched_consumers_config):
    consumer = KIAExperienceConsumer(kia_store=None)
    records = [_make_record(dims=["decisions"], insight_confidence=0.8) for _ in range(5)]

    for record in records:
        consumer.on_insight_generated(record)

    fallback = Path(patched_consumers_config.database_dir) / "layer5_experiences.jsonl"
    assert fallback.exists()

    lines = fallback.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5
    data = [json.loads(line) for line in lines]
    assert all(d["type"] == "insight_pattern" for d in data)
    assert all(d["trigger"] == "major_decision" for d in data)


def test_composite_consumer_ignores_subconsumer_exceptions():
    failing = PersonaSignalConsumer(persona_store=None)
    failing.on_insight_generated = lambda record: 1 / 0

    composite = CompositeConsumer(consumers=[failing])
    record = _make_record()
    # Should not raise despite subconsumer exception
    composite.on_insight_generated(record)


def test_persona_signal_consumer_writes_to_persona_store(tmp_path):
    store = type("FakePersonaStore", (), {"add_signal": lambda **kw: None})()
    consumer = PersonaSignalConsumer(persona_store=store)

    records = [_make_record() for _ in range(10)]
    for record in records:
        consumer.on_insight_generated(record)

    # With a working persona store and buffer size 10, all signals should be flushed
    assert consumer._signal_buffer == []
