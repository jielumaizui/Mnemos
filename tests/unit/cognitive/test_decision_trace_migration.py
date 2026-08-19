from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

import core.cognitive.decision_trace_migration as migration_module
from core.cognitive.decision_trace_migration import (
    apply_decision_trace_history_migration,
    build_decision_trace_inventory,
    configured_source_domains,
    default_source_domains,
    historical_source_input_hash,
    inspect_decision_trace_target,
    restore_decision_trace_backup,
    SourceDomain,
)
from core.cognitive.decision_trace import MaterialActionTerminal
from core.cognitive.state_contract import sha256_json
from core.ops.action_ledger import ActionLedger, make_quality_gate_observation
from core.cognitive.state_schema import (
    LEGACY_CANONICAL_V2_DDL,
    LEGACY_CANONICAL_V1_DDL_HASH,
    LEGACY_CANONICAL_V1_SCHEMA_VERSION,
    SCHEMA_COMPONENT,
    initialize_cognitive_state_schema,
    inspect_cognitive_state_schema,
    upgrade_canonical_v1_for_decision_trace_in_transaction,
    write_decision_trace_enforcement_marker,
)
from tests.cognitive_decision_fixtures import material_action_authorization


def test_configured_source_domains_preserve_external_database_paths(
    tmp_path: Path,
) -> None:
    database_dir = tmp_path / "canonical-db"
    delivery_db = tmp_path / "external-delivery" / "events.db"
    trusted_db = tmp_path / "external-trust" / "trusted.db"

    class Config:
        def __init__(self):
            self.database_dir = database_dir

        def get(self, key, default=None):
            return {
                "delivery.db_path": str(delivery_db),
                "trusted_push.db_path": str(trusted_db),
            }.get(key, default)

    domains = {
        domain.domain: domain.path
        for domain in configured_source_domains(config=Config())
    }

    assert domains == {
        "action_ledger": database_dir / "action_ledger.db",
        "delivery_events": delivery_db,
        "formal_cognitive_mutations": trusted_db,
    }


def _source_databases(root: Path) -> tuple:
    action = root / "action_ledger.db"
    delivery = root / "delivery_events.db"
    trusted = root / "trusted_push.db"
    with sqlite3.connect(action) as conn:
        conn.executescript(
            """
            CREATE TABLE action_ledger (
                action_id TEXT PRIMARY KEY,
                evidence_refs_json TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                quality_decision_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                target TEXT NOT NULL
            );
            CREATE INDEX idx_action_type ON action_ledger(target);
            """
        )
        conn.executemany(
            "INSERT INTO action_ledger VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    "act-1",
                    json.dumps(["action-evidence:1"]),
                    "{}",
                    "quality-1",
                    "2026-07-01T00:00:00+00:00",
                    "wiki://one",
                ),
                (
                    "act-2",
                    json.dumps(["action-evidence:2"]),
                    "{}",
                    "",
                    "2026-07-02T00:00:00+00:00",
                    "wiki://two",
                ),
            ),
        )
    with sqlite3.connect(delivery) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY,
                trust_decision_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decision TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO delivery_events VALUES (?, ?, ?, ?, ?)",
            (
                ("delivery-1", "trust-1", "{}", "2026-07-03", "deliver"),
                ("delivery-2", "", "{}", "2026-07-04", "suppress"),
            ),
        )
    with sqlite3.connect(trusted) as conn:
        conn.execute(
            """
            CREATE TABLE formal_cognitive_mutations (
                event_id TEXT PRIMARY KEY,
                evidence_refs TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                target_ref TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO formal_cognitive_mutations VALUES (?, ?, ?, ?, ?)",
            (
                (
                    "mutation-1",
                    json.dumps(["journal:1"]),
                    "{}",
                    "2026-07-05",
                    "kg://one",
                ),
                (
                    "mutation-2",
                    json.dumps(["journal:2"]),
                    "{}",
                    "2026-07-06",
                    "persona://two",
                ),
            ),
        )
    return default_source_domains(
        database_dir=root,
        delivery_db_path=delivery,
        trusted_push_db_path=trusted,
    )


def test_inventory_rejects_noncanonical_source_identifiers_before_sql(
    tmp_path: Path,
) -> None:
    domains = list(_source_databases(tmp_path))
    assert isinstance(domains[0], SourceDomain)
    domains[0] = replace(
        domains[0],
        table='action_ledger"; DROP TABLE action_ledger; --',
    )

    with pytest.raises(ValueError, match="non-canonical decision-trace source contract"):
        build_decision_trace_inventory(tuple(domains))


def test_inventory_excludes_only_system_proved_diagnostic_observations(
    tmp_path: Path,
) -> None:
    action = ActionLedger(
        tmp_path / "action_ledger.db",
        initialize=True,
        ownership_config=object(),
    )
    action.record_observation(
        make_quality_gate_observation(
            actor="migration-test",
            target="diagnostic:quality-gate",
            evidence_refs=("test:quality-gate",),
            details={"decision": "accept"},
        )
    )
    delivery = tmp_path / "delivery_events.db"
    with sqlite3.connect(delivery) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY,
                trust_decision_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decision TEXT NOT NULL
            )
            """
        )
    trusted = tmp_path / "trusted_push.db"
    with sqlite3.connect(trusted) as conn:
        conn.execute(
            """
            CREATE TABLE formal_cognitive_mutations (
                event_id TEXT PRIMARY KEY,
                evidence_refs TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                target_ref TEXT NOT NULL
            )
            """
        )

    inventory = build_decision_trace_inventory(
        default_source_domains(
            database_dir=tmp_path,
            delivery_db_path=delivery,
            trusted_push_db_path=trusted,
        )
    )

    assert inventory.objects == ()
    action_report = next(
        value for value in inventory.domains if value["domain"] == "action_ledger"
    )
    assert action_report["source_row_count"] == 1
    assert action_report["row_count"] == 0
    assert action_report["diagnostic_observation_count"] == 1


def _target_without_activation(root: Path) -> Path:
    target = root / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(target)
    with sqlite3.connect(target) as conn:
        conn.execute(
            "DELETE FROM mnemos_schema_registry "
            "WHERE component='decision_trace_enforcement'"
        )
    return target


def _declared_delivery_link(
    root: Path,
    *,
    source_created_at: str = "2026-07-17T10:00:00+00:00",
    decision_source_key: str = "delivery-linked",
    row_source_key: str = "delivery-linked",
) -> tuple[tuple, Path]:
    domains = _source_databases(root)
    target = root / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(target)
    source_row = {
        "event_id": row_source_key,
        "trust_decision_id": "trust-linked",
        "metadata_json": "{}",
        "created_at": source_created_at,
        "decision": "deliver",
    }
    decision_row = {**source_row, "event_id": decision_source_key}
    source_input_hash = historical_source_input_hash(
        decision_row,
        metadata_column="metadata_json",
    )
    action_input_hash = sha256_json(
        {
            "schema_version": "test.delivery_link.v1",
            "event_id": decision_source_key,
        }
    )
    authorization = material_action_authorization(
        root,
        action_type="outward_delivery",
        owner="knowledge_delivery",
        executor="knowledge_delivery_router",
        target_ref=f"delivery:test:{decision_source_key}",
        input_hash=action_input_hash,
        source_object={
            "domain": "delivery_events",
            "table": "delivery_events",
            "primary_key": "event_id",
            "primary_key_value": decision_source_key,
            "input_hash": source_input_hash,
        },
        nonce=f"declared-{decision_source_key}-{row_source_key}-{source_created_at}",
    )
    permit = authorization.permit
    state_store = authorization.coordinator.state_store
    decision_revision = state_store.revision(permit.decision_revision_id)
    assert decision_revision is not None
    decision_payload = decision_revision.payload
    snapshot_revision = state_store.revision(
        str(decision_payload["snapshot_revision_id"])
    )
    value_revision = state_store.revision(
        str(decision_payload["value_context_revision_id"])
    )
    assert snapshot_revision is not None
    assert value_revision is not None
    provenance = {
        "schema_version": "mnemos.decision_trace_provenance.v1",
        "decision_revision_id": permit.decision_revision_id,
        "decision_hash": decision_revision.payload_hash,
        "snapshot_revision_id": snapshot_revision.revision_id,
        "snapshot_hash": snapshot_revision.payload["snapshot_hash"],
        "value_context_revision_id": value_revision.revision_id,
        "value_context_hash": value_revision.payload_hash,
        "command_id": permit.command_id,
        "action_id": permit.action_id,
        "effect_id": permit.effect_id,
        "action_type": permit.action_type,
        "owner": permit.owner,
        "executor_id": permit.executor_id,
        "target_ref": permit.target_ref,
        "input_hash": permit.input_hash,
        "source_domain": "delivery_events",
        "source_table": "delivery_events",
        "source_primary_key": "event_id",
        "source_primary_key_value": decision_source_key,
        "source_input_hash": source_input_hash,
    }
    source_row["metadata_json"] = json.dumps(
        {"decision_trace_provenance": provenance},
        sort_keys=True,
    )
    with sqlite3.connect(root / "delivery_events.db") as conn:
        conn.execute(
            "INSERT INTO delivery_events VALUES (?, ?, ?, ?, ?)",
            tuple(source_row.values()),
        )
    oracle_ref = (
        "target-oracle:source-object:delivery_events:delivery_events:"
        f"event_id:{decision_source_key}:{source_input_hash}"
    )
    authorization.record_terminal(
        MaterialActionTerminal(
            status="committed",
            target_effect_id=permit.effect_id,
            before_hash=sha256_json(None),
            after_hash=source_input_hash,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"target-after:{source_input_hash}",
                oracle_ref,
            ),
            outcome="test source object committed",
            created_at="2026-07-17T11:00:00+00:00",
        )
    )
    with sqlite3.connect(target) as conn:
        conn.execute(
            "DELETE FROM mnemos_schema_registry "
            "WHERE component='decision_trace_enforcement'"
        )
    return domains, target


def _migration_payload(target: Path, source_key: str) -> dict:
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT payload_json FROM cognitive_state_migration_quarantine "
            "WHERE source_table='delivery_events.delivery_events' AND source_key=?",
            (source_key,),
        ).fetchone()
    assert row is not None
    return json.loads(str(row[0]))


def test_object_level_migration_is_dry_run_then_idempotent_apply_and_restore(
    tmp_path: Path,
) -> None:
    domains = _source_databases(tmp_path)
    target = _target_without_activation(tmp_path)
    inventory = build_decision_trace_inventory(domains)

    assert len(inventory.objects) == 6
    assert [domain["row_count"] for domain in inventory.domains] == [2, 2, 2]
    assert inspect_decision_trace_target(target)["activation_marker"] is False
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_migration_quarantine"
        ).fetchone()[0] == 0

    first = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )
    replay = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "replay-backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert first["inserted"] == 6
    assert first["existing"] == 0
    assert replay["inserted"] == 0
    assert replay["existing"] == 6
    assert first["after"]["activation_marker"] is True
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_migration_quarantine "
            "WHERE reason_code='historical_incomplete'"
        ).fetchone()[0] == 6
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions "
            "WHERE object_type IN ('value_context', 'cognitive_state_snapshot', 'decision_trace')"
        ).fetchone()[0] == 0

    restored = restore_decision_trace_backup(
        target_db=target,
        restore_manifest=Path(first["backup"]["restore_manifest"]["path"]),
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )
    assert restored["ok"] is True
    assert inspect_decision_trace_target(target)["activation_marker"] is False


def test_existing_link_requires_exact_pre_action_source_object_and_terminal_receipt(
    tmp_path: Path,
) -> None:
    domains, target = _declared_delivery_link(tmp_path)
    inventory = build_decision_trace_inventory(domains)

    applied = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert applied["linked_existing"] == 1
    assert _migration_payload(target, "delivery-linked")[
        "canonical_link_status"
    ] == "verified_existing"


def test_unrelated_valid_trace_cannot_verify_a_different_source_object(
    tmp_path: Path,
) -> None:
    domains, target = _declared_delivery_link(
        tmp_path,
        decision_source_key="delivery-other",
        row_source_key="delivery-linked",
    )
    inventory = build_decision_trace_inventory(domains)

    applied = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert applied["linked_existing"] == 0
    assert _migration_payload(target, "delivery-linked")[
        "canonical_link_status"
    ] == "declared_unresolvable"


def test_post_action_trace_cannot_retroactively_verify_history(
    tmp_path: Path,
) -> None:
    domains, target = _declared_delivery_link(
        tmp_path,
        source_created_at="2026-07-17T08:00:00+00:00",
    )
    inventory = build_decision_trace_inventory(domains)

    applied = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert applied["linked_existing"] == 0
    assert _migration_payload(target, "delivery-linked")[
        "canonical_link_status"
    ] == "declared_unresolvable"


def test_snapshot_semantic_hash_must_recompute_for_existing_link(
    tmp_path: Path,
) -> None:
    domains, target = _declared_delivery_link(tmp_path)
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT revision_id, payload_json FROM cognitive_state_revisions "
            "WHERE object_type='cognitive_state_snapshot'"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row[1]))
        payload["snapshot_hash"] = "sha256:" + "f" * 64
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='cognitive_state_revisions_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=?, payload_hash=? "
            "WHERE revision_id=?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                sha256_json(payload),
                str(row[0]),
            ),
        )
        conn.execute(str(trigger_sql))
    inventory = build_decision_trace_inventory(domains)

    applied = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert applied["linked_existing"] == 0
    assert _migration_payload(target, "delivery-linked")[
        "canonical_link_status"
    ] == "declared_unresolvable"


def test_existing_link_rejects_noncanonical_terminal_effect_hash(
    tmp_path: Path,
) -> None:
    domains, target = _declared_delivery_link(tmp_path)
    with sqlite3.connect(target) as conn:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='cognitive_state_effect_receipts_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET after_hash=?",
            ("corrupt-noncanonical-hash",),
        )
        conn.execute(str(trigger_sql))
    inventory = build_decision_trace_inventory(domains)

    applied = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert applied["linked_existing"] == 0
    assert _migration_payload(target, "delivery-linked")[
        "canonical_link_status"
    ] == "declared_unresolvable"


def test_existing_link_rejects_committed_receipt_with_terminal_failure_metadata(
    tmp_path: Path,
) -> None:
    domains, target = _declared_delivery_link(tmp_path)
    with sqlite3.connect(target) as conn:
        trigger = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='cognitive_data_consumptions' "
            "AND sql LIKE '%UPDATE%'"
        ).fetchone()
        assert trigger is not None
        conn.execute(f'DROP TRIGGER "{trigger[0]}"')
        row = conn.execute(
            "SELECT consumption_id, metadata FROM cognitive_data_consumptions "
            "WHERE status='committed'"
        ).fetchone()
        assert row is not None
        metadata = json.loads(str(row[1]))
        metadata["terminal_reason_code"] = "unexpected_terminal_reason"
        metadata["retry_exhausted"] = True
        conn.execute(
            "UPDATE cognitive_data_consumptions SET metadata=? "
            "WHERE consumption_id=?",
            (
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                str(row[0]),
            ),
        )
        conn.execute(str(trigger[1]))
    inventory = build_decision_trace_inventory(domains)

    applied = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert applied["linked_existing"] == 0
    assert _migration_payload(target, "delivery-linked")[
        "canonical_link_status"
    ] == "declared_unresolvable"


def test_restore_rejects_an_unreviewed_backup_or_drifted_target(
    tmp_path: Path,
) -> None:
    domains = _source_databases(tmp_path)
    target = _target_without_activation(tmp_path)
    inventory = build_decision_trace_inventory(domains)
    applied = apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )
    manifest = Path(applied["backup"]["restore_manifest"]["path"])

    with pytest.raises(TypeError):
        restore_decision_trace_backup(
            target_db=target,
            backup_db=Path(applied["backup"]["path"]),  # type: ignore[call-arg]
            database_dir=tmp_path,
            daemon_check=lambda _: True,
        )

    with sqlite3.connect(target) as conn:
        conn.execute(
            "INSERT INTO cognitive_state_migration_quarantine VALUES "
            "('drift', 'test', 'drift', 'test', '[]', '{}', ?, ?)",
            (
                "sha256:" + "d" * 64,
                "2026-07-17T00:00:00+00:00",
            ),
        )
    with pytest.raises(RuntimeError, match="target drifted"):
        restore_decision_trace_backup(
            target_db=target,
            restore_manifest=manifest,
            database_dir=tmp_path,
            daemon_check=lambda _: True,
        )


def test_source_drift_fails_before_backup_or_target_write(tmp_path: Path) -> None:
    domains = _source_databases(tmp_path)
    target = _target_without_activation(tmp_path)
    inventory = build_decision_trace_inventory(domains)
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        conn.execute(
            "UPDATE delivery_events SET decision='changed' WHERE event_id='delivery-1'"
        )
    backup_dir = tmp_path / "should-not-exist"

    with pytest.raises(RuntimeError, match="inventory drifted"):
        apply_decision_trace_history_migration(
            domains=domains,
            target_db=target,
            expected_inventory_hash=inventory.inventory_hash,
            backup_dir=backup_dir,
            database_dir=tmp_path,
            daemon_check=lambda _: True,
        )

    assert not backup_dir.exists()
    assert inspect_decision_trace_target(target)["activation_marker"] is False


def test_apply_failure_rolls_back_inventory_and_activation(tmp_path: Path) -> None:
    domains = _source_databases(tmp_path)
    target = _target_without_activation(tmp_path)
    inventory = build_decision_trace_inventory(domains)

    def failpoint(stage: str) -> None:
        if stage == "after_inventory":
            raise OSError("injected migration crash")

    with pytest.raises(OSError, match="injected migration crash"):
        apply_decision_trace_history_migration(
            domains=domains,
            target_db=target,
            expected_inventory_hash=inventory.inventory_hash,
            backup_dir=tmp_path / "backups",
            database_dir=tmp_path,
            daemon_check=lambda _: True,
            failpoint=failpoint,
        )

    state = inspect_decision_trace_target(target)
    assert state["activation_marker"] is False
    assert state["historical_incomplete_count"] == 0


def test_restore_manifest_is_durable_before_the_target_commit(
    tmp_path: Path,
) -> None:
    domains = _source_databases(tmp_path)
    target = _target_without_activation(tmp_path)
    inventory = build_decision_trace_inventory(domains)

    def failpoint(stage: str) -> None:
        if stage == "after_restore_manifest":
            raise OSError("crash after durable restore manifest")

    with pytest.raises(OSError, match="durable restore manifest"):
        apply_decision_trace_history_migration(
            domains=domains,
            target_db=target,
            expected_inventory_hash=inventory.inventory_hash,
            backup_dir=tmp_path / "backups",
            database_dir=tmp_path,
            daemon_check=lambda _: True,
            failpoint=failpoint,
        )

    state = inspect_decision_trace_target(target)
    assert state["activation_marker"] is False
    assert state["historical_incomplete_count"] == 0
    assert not list((tmp_path / "backups").glob("*.restore.json"))


def test_backup_and_restore_manifest_fsync_their_parent_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domains = _source_databases(tmp_path)
    target = _target_without_activation(tmp_path)
    inventory = build_decision_trace_inventory(domains)
    fsynced_directories: list[Path] = []
    original_fsync_directory = migration_module._fsync_directory

    def observed_fsync_directory(path: Path) -> None:
        fsynced_directories.append(Path(path).resolve(strict=True))
        original_fsync_directory(path)

    monkeypatch.setattr(
        migration_module,
        "_fsync_directory",
        observed_fsync_directory,
    )

    apply_decision_trace_history_migration(
        domains=domains,
        target_db=target,
        expected_inventory_hash=inventory.inventory_hash,
        backup_dir=tmp_path / "backups",
        database_dir=tmp_path,
        daemon_check=lambda _: True,
    )

    assert tmp_path.resolve(strict=True) in fsynced_directories
    assert (tmp_path / "backups").resolve(strict=True) in fsynced_directories


def test_restore_manifest_write_failure_cannot_leave_a_committed_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domains = _source_databases(tmp_path)
    target = _target_without_activation(tmp_path)
    inventory = build_decision_trace_inventory(domains)

    def fail_manifest(**_kwargs):
        raise OSError("manifest fsync failed")

    monkeypatch.setattr(migration_module, "_write_restore_manifest", fail_manifest)
    with pytest.raises(OSError, match="manifest fsync failed"):
        apply_decision_trace_history_migration(
            domains=domains,
            target_db=target,
            expected_inventory_hash=inventory.inventory_hash,
            backup_dir=tmp_path / "backups",
            database_dir=tmp_path,
            daemon_check=lambda _: True,
        )

    state = inspect_decision_trace_target(target)
    assert state["activation_marker"] is False
    assert state["historical_incomplete_count"] == 0


def test_exact_canonical_v1_schema_upgrades_losslessly_inside_caller_transaction(
    tmp_path: Path,
) -> None:
    target = tmp_path / "producer_consumer_ledger.db"
    legacy_ddl = LEGACY_CANONICAL_V2_DDL.replace(
        """AND (
            json_array_length(intended_consumers) > 0
            OR data_type='decision_trace'
        )""",
        "AND json_array_length(intended_consumers) > 0",
    ).replace(
        """'committed', 'failed_terminal', 'intentional_skip', 'rejected',
        'revoked', 'dead_letter', 'expired', 'superseded'""",
        """'committed', 'intentional_skip', 'rejected',
        'dead_letter', 'expired', 'superseded'""",
    ).replace(
        """'committed', 'failed_terminal', 'intentional_skip', 'rejected',
        'revoked', 'dead_letter'""",
        "'committed', 'intentional_skip', 'rejected', 'dead_letter'",
    )
    with sqlite3.connect(target) as conn:
        conn.executescript(legacy_ddl)
        conn.execute(
            "INSERT INTO mnemos_schema_registry VALUES (?, ?, ?, ?)",
            (
                SCHEMA_COMPONENT,
                LEGACY_CANONICAL_V1_SCHEMA_VERSION,
                LEGACY_CANONICAL_V1_DDL_HASH,
                "2026-07-16T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_migration_quarantine VALUES (
                'cogquarantine-existing', 'legacy.table', 'row-1',
                'legacy_reason', '[]', '{}', ?, '2026-07-16T00:00:00+00:00'
            )
            """,
            ("sha256:" + "a" * 64,),
        )
        conn.commit()
        assert inspect_cognitive_state_schema(conn).classification == (
            "canonical_v1_decision_trace_upgrade_required"
        )
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        counts = upgrade_canonical_v1_for_decision_trace_in_transaction(conn)
        write_decision_trace_enforcement_marker(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.commit()

    with sqlite3.connect(target) as conn:
        assert inspect_cognitive_state_schema(conn).classification == "canonical"
        assert counts["cognitive_state_migration_quarantine"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_migration_quarantine"
        ).fetchone()[0] == 1
