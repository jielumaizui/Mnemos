from __future__ import annotations

from contextlib import contextmanager

import pytest

from scripts import reconcile_cognitive_projection_lifecycle as reconcile_module
from scripts.audit_cognitive_projection_lifecycle import (
    _AuditConfig,
    _initialize_empty_canonical_stores,
)
from scripts.reconcile_cognitive_projection_lifecycle import (
    _build_sparse_shadow,
    apply_reconciliation,
    build_reconciliation_plan,
)


def test_dry_run_builds_exact_plan_without_touching_production_targets(tmp_path):
    config = _AuditConfig(tmp_path)
    _initialize_empty_canonical_stores(config)
    stale = config.wiki_dir / "L3-Observations" / "stale-dimension.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale projection", encoding="utf-8")
    formal = config.wiki_dir / "03-Tech" / "Python.md"
    formal.parent.mkdir(parents=True, exist_ok=True)
    formal.write_text("---\ntitle: Python\n---\n", encoding="utf-8")
    projection_db = config.database_dir / "wiki_projection.db"
    before_stale = stale.read_bytes()
    assert not projection_db.exists()

    first = build_reconciliation_plan(
        config=config,
        database_dir=config.database_dir,
        wiki_dir=config.wiki_dir,
    )
    second = build_reconciliation_plan(
        config=config,
        database_dir=config.database_dir,
        wiki_dir=config.wiki_dir,
    )

    assert first["ok"] is True
    assert first["production_mutation_count"] == 0
    assert first["plan_hash"] == second["plan_hash"]
    assert first["counts"]["delete"] == 1
    assert any(
        operation["relative_path"] == "L3-Observations/stale-dimension.md"
        and operation["action"] == "delete"
        for operation in first["operations"]
    )
    assert stale.read_bytes() == before_stale
    assert formal.is_file()
    assert not projection_db.exists()


def test_apply_revalidates_reviewed_plan_only_after_offline_lock(
    tmp_path,
    monkeypatch,
):
    config = _AuditConfig(tmp_path)
    config.database_dir.mkdir(parents=True, exist_ok=True)
    lock_state = {"held": False, "plan_calls": 0}

    @contextmanager
    def fake_offline_lock(_database_dir):
        lock_state["held"] = True
        try:
            yield
        finally:
            lock_state["held"] = False

    def fake_plan(**_kwargs):
        assert lock_state["held"] is True
        lock_state["plan_calls"] += 1
        return {"plan_hash": "sha256:current"}

    monkeypatch.setattr(
        "core.ops.offline_migration_lock.offline_migration_lock",
        fake_offline_lock,
    )
    monkeypatch.setattr(reconcile_module, "build_reconciliation_plan", fake_plan)

    with pytest.raises(RuntimeError, match="does not match locked state"):
        apply_reconciliation(
            config=config,
            database_dir=config.database_dir,
            wiki_dir=config.wiki_dir,
            expected_plan_hash="sha256:reviewed",
            backup_dir=tmp_path / "backup",
        )

    assert lock_state == {"held": False, "plan_calls": 1}
    assert not (tmp_path / "backup").exists()


def test_apply_binds_lifecycle_and_consumers_to_requested_config(
    tmp_path,
    monkeypatch,
):
    config = _AuditConfig(tmp_path)
    config.database_dir.mkdir(parents=True, exist_ok=True)
    calls = {"registered": 0, "subscribed": 0, "started": 0, "closed": 0}
    buses = []

    @contextmanager
    def fake_offline_lock(_database_dir):
        yield

    class FakeBus:
        def __init__(self, *, config, **_kwargs):
            self.config = config
            self.projection_db_path = config.database_dir / "wiki_projection.db"
            buses.append(self)

        def start_dispatch(self):
            calls["started"] += 1

        def stop_dispatch(self):
            return None

        def close(self):
            calls["closed"] += 1

    class FakeGraphStore:
        def __init__(self, db_path, ownership_config=None):
            assert db_path == str(config.database_dir / "cognitive_graph.db")
            assert ownership_config.database_dir == config.database_dir

        def close(self):
            return None

    class FakeUpdater:
        def __init__(self, *, store, bus):
            assert isinstance(store, FakeGraphStore)
            assert bus is buses[0]

        def subscribe(self):
            calls["subscribed"] += 1

    def fake_register(bus, bound_config, **kwargs):
        assert bus is buses[0]
        assert bound_config.database_dir == config.database_dir
        assert kwargs["projection_lifecycle"].ledger.db_path == (
            config.database_dir / "wiki_projection.db"
        )
        calls["registered"] += 1

    def fake_sync(**kwargs):
        assert kwargs["config"].database_dir == config.database_dir
        assert kwargs["lifecycle"].event_bus is buses[0]
        assert kwargs["lifecycle"].ledger.db_path == (
            config.database_dir / "wiki_projection.db"
        )
        return {"status": "ok"}

    monkeypatch.setattr(
        "core.ops.offline_migration_lock.offline_migration_lock",
        fake_offline_lock,
    )
    monkeypatch.setattr(
        reconcile_module,
        "build_reconciliation_plan",
        lambda **_kwargs: {
            "plan_hash": "sha256:reviewed",
            "counts": {},
            "desired_manifest": {},
        },
    )
    monkeypatch.setattr(
        reconcile_module,
        "_create_backup",
        lambda **_kwargs: {"status": "created"},
    )
    monkeypatch.setattr(
        reconcile_module,
        "_canonical_source_hashes",
        lambda _database_dir: {"canonical": "sha256:same"},
    )
    monkeypatch.setattr(reconcile_module, "sync_all_projections", fake_sync)
    monkeypatch.setattr(
        reconcile_module,
        "_verify_applied_manifest",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        reconcile_module,
        "audit_live_projection_state",
        lambda **_kwargs: {
            "initialized": True,
            "projection_binding_gap": 0,
            "stale_projection": 0,
            "required_consumer_receipt_gap": 0,
        },
    )
    monkeypatch.setattr("core.mnemos_bus.EventBus", FakeBus)
    monkeypatch.setattr(
        "daemon.wiki_projection_handlers.register_wiki_projection_handlers",
        fake_register,
    )
    monkeypatch.setattr("core.cognitive_graph.CognitiveGraphStore", FakeGraphStore)
    monkeypatch.setattr("core.cognitive_graph.CognitiveGraphUpdater", FakeUpdater)

    result = apply_reconciliation(
        config=config,
        database_dir=config.database_dir,
        wiki_dir=config.wiki_dir,
        expected_plan_hash="sha256:reviewed",
        backup_dir=tmp_path / "backup",
    )

    assert result["ok"] is True
    assert calls == {"registered": 1, "subscribed": 1, "started": 1, "closed": 1}


def test_shadow_vault_copies_non_projection_pages_inside_its_boundary(tmp_path):
    source = tmp_path / "source"
    shadow = tmp_path / "shadow"
    formal = source / "03-Tech" / "source.md"
    formal.parent.mkdir(parents=True)
    formal.write_text("# Canonical source\n", encoding="utf-8")

    assert _build_sparse_shadow(source, shadow) == 1

    mirrored = shadow / "03-Tech" / "source.md"
    assert mirrored.read_bytes() == formal.read_bytes()
    assert mirrored.is_symlink() is False
    assert mirrored.resolve().is_relative_to(shadow.resolve())
