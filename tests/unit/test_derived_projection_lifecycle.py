from __future__ import annotations

from pathlib import Path

import pytest

from core.wiki_derived_projection import (
    DerivedProjectionLifecycle,
    ProjectionPageSpec,
)
from core.wiki_projection_lifecycle import WikiProjectionLedger


class RecordingBus:
    def __init__(self, *, failures: int = 0):
        self.failures = failures
        self.events = []

    def publish(self, event):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("injected publisher failure")
        self.events.append(event)
        return event.trace_id


def _page(vault: Path, name: str, body: str = "# Projection\n") -> ProjectionPageSpec:
    return ProjectionPageSpec(
        path=vault / "L3-Observations" / f"{name}.md",
        content=body,
        page_role="formal_derived:observation",
        canonical_revision=f"observation-revision:{name}",
        source_refs=(f"observation:{name}",),
    )


def _lifecycle(
    tmp_path: Path,
    *,
    bus: RecordingBus | None = None,
    writer=None,
) -> tuple[DerivedProjectionLifecycle, Path, WikiProjectionLedger]:
    vault = tmp_path / "vault"
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    lifecycle = DerivedProjectionLifecycle(
        vault,
        ledger=ledger,
        event_bus=bus or RecordingBus(),
        file_writer=writer,
    )
    return lifecycle, vault, ledger


def test_generation_binds_canonical_revision_content_and_publisher_receipt(tmp_path):
    bus = RecordingBus()
    lifecycle, vault, ledger = _lifecycle(tmp_path, bus=bus)

    generation = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[_page(vault, "attention")],
        full=True,
    )

    assert generation.status == "committed"
    assert generation.expected_item_count == 1
    item = generation.items[0]
    assert item.canonical_revision == "observation-revision:attention"
    assert item.content_sha256.startswith("sha256:")
    assert item.mutation_id
    assert item.page_revision
    assert item.event_trace_id == item.mutation_id
    assert item.status == "published"
    assert ledger.mutation_receipt(item.mutation_id).event_trace_id == item.mutation_id
    binding = lifecycle.binding_for_path(_page(vault, "attention").path)
    assert binding is not None
    assert binding["generation_id"] == generation.generation_id
    assert binding["canonical_revision"] == "observation-revision:attention"
    content = _page(vault, "attention").path.read_text(encoding="utf-8")
    assert 'page_role: "formal_derived:observation"' in content
    assert 'canonical_revision: "observation-revision:attention"' in content
    assert len(bus.events) == 1


def test_custom_ledger_requires_explicit_event_bus(tmp_path, monkeypatch):
    from core import mnemos_bus

    fallback_calls = []
    monkeypatch.setattr(
        mnemos_bus,
        "publish_event",
        lambda *_args, **_kwargs: fallback_calls.append((_args, _kwargs)),
    )
    vault = tmp_path / "vault"
    lifecycle = DerivedProjectionLifecycle(
        vault,
        ledger=WikiProjectionLedger(tmp_path / "custom" / "wiki_projection.db"),
    )

    with pytest.raises(RuntimeError, match="explicit EventBus"):
        lifecycle.publish_generation(
            projection_kind="observation",
            scope_root=vault / "L3-Observations",
            pages=[_page(vault, "attention")],
            full=True,
        )

    assert fallback_calls == []


def test_a_b_a_replay_reactivates_the_latest_binding(tmp_path):
    lifecycle, vault, _ledger = _lifecycle(tmp_path)
    path = vault / "L3-Observations" / "attention.md"

    def version(label: str) -> ProjectionPageSpec:
        return ProjectionPageSpec(
            path=path,
            content=f"# Projection {label}\n",
            page_role="formal_derived:observation",
            canonical_revision=f"canonical:{label}",
            source_refs=(f"observation:{label}",),
        )

    first = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=path.parent,
        pages=[version("A")],
        full=True,
    )
    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=path.parent,
        pages=[version("B")],
        full=True,
    )
    replay = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=path.parent,
        pages=[version("A")],
        full=True,
    )

    assert replay.generation_id == first.generation_id
    binding = lifecycle.binding_for_path(path)
    assert binding is not None
    assert binding["generation_id"] == replay.generation_id
    assert binding["canonical_revision"] == "canonical:A"
    assert binding["content_sha256"] == replay.items[0].content_sha256
    assert lifecycle.stale_paths(
        projection_kind="observation",
        scope_root=path.parent,
    ) == []


def test_nth_file_failure_is_replayable_without_duplicate_mutations(tmp_path):
    calls = {"count": 0}
    observed_authorizations = []

    def fail_second(authorization, path: Path, content: str) -> None:
        calls["count"] += 1
        observed_authorizations.append(authorization)
        assert ledger.mutation_receipt(authorization.mutation_id) is not None
        if calls["count"] == 2:
            raise OSError("injected second-file failure")
        authorization.assert_upsert(path, content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    lifecycle, vault, ledger = _lifecycle(tmp_path, writer=fail_second)
    pages = [_page(vault, "attention"), _page(vault, "time")]

    with pytest.raises(OSError, match="second-file failure"):
        lifecycle.publish_generation(
            projection_kind="observation",
            scope_root=vault / "L3-Observations",
            pages=pages,
            full=True,
        )

    assert pages[0].path.is_file()
    assert not pages[1].path.exists()
    assert len(ledger.list_mutations()) == 2
    assert all(
        ledger.mutation_receipt(authorization.mutation_id) is not None
        for authorization in observed_authorizations
    )

    replay = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=pages,
        full=True,
    )

    assert replay.status == "committed"
    assert all(page.path.is_file() for page in pages)
    assert len(ledger.list_mutations()) == 2


def test_publisher_failure_leaves_durable_mutation_for_restart_replay(tmp_path):
    bus = RecordingBus(failures=1)
    lifecycle, vault, ledger = _lifecycle(tmp_path, bus=bus)
    page = _page(vault, "attention")

    with pytest.raises(RuntimeError, match="publisher failure"):
        lifecycle.publish_generation(
            projection_kind="observation",
            scope_root=vault / "L3-Observations",
            pages=[page],
            full=True,
        )

    assert page.path.is_file()
    assert len(ledger.unpublished_mutations()) == 1
    mutation_id = ledger.unpublished_mutations()[0].mutation_id

    restarted = DerivedProjectionLifecycle(
        vault,
        ledger=WikiProjectionLedger(ledger.db_path),
        event_bus=bus,
    )
    replay = restarted.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[page],
        full=True,
    )

    assert replay.status == "committed"
    assert replay.items[0].mutation_id == mutation_id
    assert replay.items[0].event_trace_id == mutation_id
    assert len(ledger.list_mutations()) == 1


def test_full_generation_tombstones_and_removes_stale_projection(tmp_path):
    lifecycle, vault, ledger = _lifecycle(tmp_path)
    attention = _page(vault, "attention")
    time_page = _page(vault, "time")
    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[attention, time_page],
        full=True,
    )

    second = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[attention],
        full=True,
    )

    assert attention.path.is_file()
    assert not time_page.path.exists()
    stale = next(item for item in second.items if item.path == str(time_page.path.resolve()))
    assert stale.action == "delete"
    assert stale.status == "published"
    assert stale.event_trace_id == stale.mutation_id
    identity = ledger.page_identity(time_page.path)
    assert identity is not None
    assert identity["lifecycle_state"] == "tombstone"
    assert lifecycle.stale_paths(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
    ) == []


def test_delete_event_is_published_only_after_physical_removal(tmp_path):
    observed_exists = []

    class DeletionOrderBus(RecordingBus):
        def publish(self, event):
            if event.payload.get("mutation_type") == "delete":
                observed_exists.append(Path(event.payload["page_path"]).exists())
            return super().publish(event)

    bus = DeletionOrderBus()
    lifecycle, vault, _ledger = _lifecycle(tmp_path, bus=bus)
    page = _page(vault, "attention")
    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[page],
        full=True,
    )

    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[],
        full=True,
    )

    assert observed_exists == [False]


def test_full_and_incremental_publication_render_identical_bytes(tmp_path):
    full_root = tmp_path / "full"
    incremental_root = tmp_path / "incremental"
    full = DerivedProjectionLifecycle(
        full_root,
        ledger=WikiProjectionLedger(tmp_path / "full.db"),
        event_bus=RecordingBus(),
    )
    incremental = DerivedProjectionLifecycle(
        incremental_root,
        ledger=WikiProjectionLedger(tmp_path / "incremental.db"),
        event_bus=RecordingBus(),
    )
    full_page = _page(full_root, "attention", "# Stable projection\n")
    incremental_page = _page(incremental_root, "attention", "# Stable projection\n")

    full.publish_generation(
        projection_kind="observation",
        scope_root=full_root / "L3-Observations",
        pages=[full_page],
        full=True,
    )
    incremental.publish_generation(
        projection_kind="observation",
        scope_root=incremental_root / "L3-Observations",
        pages=[incremental_page],
        full=False,
    )

    assert full_page.path.read_bytes() == incremental_page.path.read_bytes()


def test_replaying_same_manifest_records_untracked_file_drift_repair(tmp_path):
    bus = RecordingBus()
    lifecycle, vault, ledger = _lifecycle(tmp_path, bus=bus)
    page = _page(vault, "attention", "# Canonical projection\n")
    first = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[page],
        full=True,
    )
    page.path.write_text("# Untracked drift\n", encoding="utf-8")

    replay = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[page],
        full=True,
    )

    assert "Canonical projection" in page.path.read_text(encoding="utf-8")
    assert len(ledger.list_mutations()) == 2
    assert len(bus.events) == 2
    assert replay.items[0].mutation_id != first.items[0].mutation_id


def test_replaying_delete_manifest_refuses_changed_resurrected_file(tmp_path):
    bus = RecordingBus()
    lifecycle, vault, ledger = _lifecycle(tmp_path, bus=bus)
    page = _page(vault, "attention")
    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[page],
        full=True,
    )
    first_delete = lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[],
        full=True,
    )
    page.path.write_text("# Resurrected stale file\n", encoding="utf-8")

    with pytest.raises(ValueError, match="delete content hash mismatch"):
        lifecycle.publish_generation(
            projection_kind="observation",
            scope_root=vault / "L3-Observations",
            pages=[],
            full=True,
        )

    assert page.path.is_file()
    assert len(ledger.list_mutations()) == 3
    delete_events = [
        event for event in bus.events if event.payload["mutation_type"] == "delete"
    ]
    assert len(delete_events) == 1
    assert ledger.unpublished_mutations()[-1].mutation_id != first_delete.items[0].mutation_id


def test_managed_full_generation_preserves_nested_independent_reports(tmp_path):
    lifecycle, vault, _ledger = _lifecycle(tmp_path)
    dimension = _page(vault, "attention")
    independent = vault / "L3-Observations" / "immune" / "report.md"
    independent.parent.mkdir(parents=True, exist_ok=True)
    independent.write_text("independent report", encoding="utf-8")

    lifecycle.publish_generation(
        projection_kind="observation",
        scope_root=vault / "L3-Observations",
        pages=[dimension],
        full=True,
        owned_paths=(dimension.path,),
    )

    assert independent.read_text(encoding="utf-8") == "independent report"
