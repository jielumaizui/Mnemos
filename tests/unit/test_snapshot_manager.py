import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.backup.snapshot_manager import MnemosSnapshotManager, audit_backup_recovery_contract
from core.ops.producer_consumer_ledger import ProducerConsumerLedger
from core.ops.runtime_flow_telemetry import record_runtime_produced


class FakeConfig:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.database_dir.mkdir(parents=True)
        self.config_path = root / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        with sqlite3.connect(self.database_dir / "raw_events.db") as conn:
            conn.execute("CREATE TABLE raw_events (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO raw_events VALUES ('subject-row')")
        self._vaults = {"mnemos": root / "mnemos_vault", "raw": root / "raw_vault"}
        for vault in self._vaults.values():
            vault.mkdir()
            (vault / "page.md").write_text("hello", encoding="utf-8")

    def vault_dir(self, name: str) -> Path:
        return self._vaults[name]


def test_snapshot_contract_audit_passes():
    assert audit_backup_recovery_contract(strict=True) == []


def test_snapshot_create_dry_run_manifest_has_checksums(tmp_path):
    cfg = FakeConfig(tmp_path)
    manifest = MnemosSnapshotManager(cfg).create(reason="test", dry_run=True)

    assert manifest.dry_run is True
    assert manifest.file_entries
    assert manifest.database_entries
    database_names = {Path(entry.source_path).name for entry in manifest.database_entries}
    assert "raw_events.db" in database_names
    assert "raw_events.db.enc" not in database_names
    assert manifest.validate() == []


def test_data_delete_snapshot_is_bound_retained_and_payload_verified(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)

    manifest = manager.create_data_delete_snapshot(
        scope_kind="session",
        scope_value="private-session",
        retention_days=30,
    )
    verification = manager.verify_data_delete_snapshot(
        manifest.snapshot_id,
        scope_kind="session",
        scope_value="private-session",
    )

    assert manifest.schema_version == "mnemos.snapshot_manifest.v2"
    assert manifest.trigger_action == "data_delete.apply"
    assert manifest.operation_binding_hash.startswith("sha256:")
    assert manifest.retention_expires_at
    assert verification["valid"] is True
    assert verification["status"] == "verified"
    assert verification["retention_status"] == "retained_until"
    assert verification["payload_count"] > 0
    assert verification["errors"] == []


def test_data_delete_snapshot_rejects_wrong_subject_and_payload_tampering(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)
    manifest = manager.create_data_delete_snapshot(
        scope_kind="session",
        scope_value="private-session",
        retention_days=30,
    )

    wrong_subject = manager.verify_data_delete_snapshot(
        manifest.snapshot_id,
        scope_kind="session",
        scope_value="another-session",
    )
    payload = Path(manifest.manifest_path).parent / manifest.file_entries[0].snapshot_path
    payload.write_bytes(payload.read_bytes() + b"tampered")
    tampered = manager.verify_data_delete_snapshot(
        manifest.snapshot_id,
        scope_kind="session",
        scope_value="private-session",
    )

    assert wrong_subject["valid"] is False
    assert "operation_binding_mismatch" in wrong_subject["errors"]
    assert tampered["valid"] is False
    assert "payload_checksum_mismatch" in tampered["errors"]


def test_data_delete_snapshot_expires_and_is_only_pruned_explicitly(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)
    manifest = manager.create_data_delete_snapshot(
        scope_kind="session",
        scope_value="private-session",
        retention_days=1,
    )
    future = datetime.now(timezone.utc) + timedelta(days=2)

    expired = manager.verify_data_delete_snapshot(
        manifest.snapshot_id,
        scope_kind="session",
        scope_value="private-session",
        now=future,
    )
    preview = manager.prune_expired_data_delete_snapshots(now=future, apply=False)

    assert expired["valid"] is False
    assert expired["retention_status"] == "expired"
    assert Path(manifest.manifest_path).exists()
    assert preview["candidate_snapshot_ids"] == [manifest.snapshot_id]
    assert preview["deleted_snapshot_ids"] == []

    applied = manager.prune_expired_data_delete_snapshots(now=future, apply=True)
    assert applied["deleted_snapshot_ids"] == [manifest.snapshot_id]
    assert not Path(manifest.manifest_path).exists()


def test_missing_snapshot_yields_a_blocked_terminal_restore_plan(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)
    snapshot_id = "snap-missing-manifest"
    ProducerConsumerLedger(cfg, initialize=True)
    production_event_id = record_runtime_produced(
        "snapshot_manifest_to_restore_plan",
        source="core/backup/snapshot_manager.py",
        item_id=snapshot_id,
        intended_consumers=["core/backup/snapshot_manager.py:restore_plan"],
        metadata={"transition": "snapshot_manifest_committed"},
        config_or_path=cfg.database_dir,
    )

    plan = manager.restore_plan(snapshot_id)
    flow = ProducerConsumerLedger(cfg, initialize=False).snapshot()["flows"][
        "snapshot_manifest_to_restore_plan"
    ]

    assert production_event_id
    assert plan.status == "blocked"
    assert plan.operations == ()
    assert plan.conflicts == ("snapshot_manifest_missing",)
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    assert flow["consumed_count"] == 1


def test_missing_snapshot_without_a_producer_does_not_create_an_orphan_receipt(
    tmp_path,
):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)
    ProducerConsumerLedger(cfg, initialize=True)

    plan = manager.restore_plan("snap-never-produced")
    with sqlite3.connect(cfg.database_dir / "producer_consumer_ledger.db") as conn:
        receipt_count = int(
            conn.execute("SELECT COUNT(*) FROM runtime_flow_receipts").fetchone()[0]
        )

    assert plan.status == "blocked"
    assert plan.conflicts == ("snapshot_manifest_missing",)
    assert receipt_count == 0


def test_restore_plan_rejects_a_snapshot_path_escape(tmp_path):
    cfg = FakeConfig(tmp_path)
    manager = MnemosSnapshotManager(cfg)

    try:
        manager.restore_plan("../outside")
    except ValueError as exc:
        assert str(exc) == "invalid snapshot_id"
    else:
        raise AssertionError("path-escaping snapshot id was accepted")
