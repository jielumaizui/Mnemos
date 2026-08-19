from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_raw_projection_backups import audit_raw_projection_backups


def test_backup_audit_is_metadata_only_and_marks_legacy_full_copy(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    legacy = backup_root / "raw-vault-projection-legacy"
    legacy.mkdir(parents=True)
    raw_copy = legacy / "session.md"
    raw_copy.write_text("private raw evidence", encoding="utf-8")
    metadata_only = backup_root / "raw-vault-projection-v2"
    metadata_only.mkdir()
    plan_hash = "a" * 64
    generation_hash = "b" * 64
    (metadata_only / f"raw-projection-plan-{plan_hash}.json").write_text(
        json.dumps(
            {
                "schema_version": "mnemos.raw_projection_change_set.v1",
                "status": "planned",
                "plan_hash": plan_hash,
                "generation_hash": generation_hash,
                "backup_dir": str(metadata_only.resolve()),
                "changed_paths": ["codex/day/chunk.md"],
                "stale_paths": [],
                "index_changed_paths": ["codex/day/chunk.md"],
                "index_deleted_paths": [],
            }
        ),
        encoding="utf-8",
    )
    (metadata_only / f"raw-projection-commit-{plan_hash}.json").write_text(
        json.dumps(
            {
                "schema_version": "mnemos.raw_projection_change_set.v1",
                "status": "committed",
                "plan_hash": plan_hash,
                "generation_hash": generation_hash,
                "changed_paths": ["codex/day/chunk.md"],
                "stale_paths": [],
                "index_changed_paths": ["codex/day/chunk.md"],
                "index_deleted_paths": [],
                "bytes_written": 42,
            }
        ),
        encoding="utf-8",
    )
    before = raw_copy.stat().st_mtime_ns

    report = audit_raw_projection_backups(backup_root, max_total_bytes=1)

    by_id = {item["backup_id"]: item for item in report["backups"]}
    assert report["ok"] is False
    assert report["destructive_actions"] == 0
    assert report["deletion_authorized"] is False
    assert by_id["raw-vault-projection-legacy"]["recovery_class"] == "legacy_full_copy"
    assert by_id["raw-vault-projection-v2"]["recovery_class"] == "metadata_only"
    assert by_id["raw-vault-projection-v2"]["manifest"]["count"] == 2
    assert by_id["raw-vault-projection-v2"]["manifest"]["set_hash"]
    assert raw_copy.read_text(encoding="utf-8") == "private raw evidence"
    assert raw_copy.stat().st_mtime_ns == before


def test_backup_audit_keeps_invalid_or_unrecognized_files_out_of_metadata_only_class(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    invalid = backup_root / "raw-vault-projection-invalid"
    invalid.mkdir(parents=True)
    (invalid / f"raw-projection-plan-{'b' * 64}.json").write_text(
        '{"schema_version":"wrong"}',
        encoding="utf-8",
    )
    extra = backup_root / "raw-vault-projection-extra"
    extra.mkdir()
    (extra / f"raw-projection-commit-{'c' * 64}.json").write_text(
        '{"schema_version":"mnemos.raw_projection_change_set.v1"}',
        encoding="utf-8",
    )
    (extra / "unrecognized.json").write_text("{}", encoding="utf-8")

    report = audit_raw_projection_backups(backup_root, max_total_bytes=1024)

    by_id = {item["backup_id"]: item for item in report["backups"]}
    assert (
        by_id["raw-vault-projection-invalid"]["recovery_class"]
        == "metadata_incomplete"
    )
    assert by_id["raw-vault-projection-invalid"]["manifest"]["valid"] is False
    assert by_id["raw-vault-projection-extra"]["recovery_class"] == "legacy_full_copy"


def test_backup_audit_rejects_unpaired_or_wrong_identity_receipts(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    planned_only = backup_root / "raw-vault-projection-planned-only"
    planned_only.mkdir(parents=True)
    plan_hash = "d" * 64
    generation_hash = "e" * 64
    payload = {
        "schema_version": "mnemos.raw_projection_change_set.v1",
        "status": "planned",
        "plan_hash": plan_hash,
        "generation_hash": generation_hash,
        "backup_dir": str(planned_only.resolve()),
        "changed_paths": [],
        "stale_paths": [],
        "index_changed_paths": [],
        "index_deleted_paths": [],
    }
    (planned_only / f"raw-projection-plan-{plan_hash}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (planned_only / f"raw-projection-change-{plan_hash}.json").write_text(
        json.dumps(
            {
                "schema_version": "mnemos.raw_projection_change_set.v1",
                "generation_hash": plan_hash,
                "changed_paths": [],
                "unchanged_paths": [],
                "stale_paths": [],
                "bytes_written": 0,
            }
        ),
        encoding="utf-8",
    )
    wrong_identity = backup_root / "raw-vault-projection-wrong-identity"
    wrong_identity.mkdir()
    (wrong_identity / f"raw-projection-plan-{'f' * 64}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    report = audit_raw_projection_backups(backup_root, max_total_bytes=1024 * 1024)

    by_id = {item["backup_id"]: item for item in report["backups"]}
    assert (
        by_id["raw-vault-projection-planned-only"]["recovery_class"]
        == "metadata_incomplete"
    )
    assert by_id["raw-vault-projection-planned-only"]["manifest"]["valid"] is False
    assert (
        by_id["raw-vault-projection-wrong-identity"]["recovery_class"]
        == "metadata_incomplete"
    )


def test_backup_audit_rejects_extra_receipt_payload_or_wrong_backup_scope(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    plan_hash = "4" * 64
    generation_hash = "5" * 64
    for name, corrupt in (
        ("raw-vault-projection-extra-payload", "extra"),
        ("raw-vault-projection-wrong-scope", "scope"),
    ):
        backup = backup_root / name
        backup.mkdir(parents=True)
        plan = {
            "schema_version": "mnemos.raw_projection_change_set.v1",
            "status": "planned",
            "plan_hash": plan_hash,
            "generation_hash": generation_hash,
            "backup_dir": (
                str((tmp_path / "elsewhere").resolve())
                if corrupt == "scope"
                else str(backup.resolve())
            ),
            "changed_paths": [],
            "stale_paths": [],
            "index_changed_paths": [],
            "index_deleted_paths": [],
        }
        if corrupt == "extra":
            plan["raw_body"] = "must never be accepted"
        commit = {
            "schema_version": "mnemos.raw_projection_change_set.v1",
            "status": "committed",
            "plan_hash": plan_hash,
            "generation_hash": generation_hash,
            "changed_paths": [],
            "stale_paths": [],
            "index_changed_paths": [],
            "index_deleted_paths": [],
            "bytes_written": 0,
        }
        (backup / f"raw-projection-plan-{plan_hash}.json").write_text(
            json.dumps(plan),
            encoding="utf-8",
        )
        (backup / f"raw-projection-commit-{plan_hash}.json").write_text(
            json.dumps(commit),
            encoding="utf-8",
        )

    report = audit_raw_projection_backups(backup_root, max_total_bytes=1024 * 1024)

    assert all(
        record["recovery_class"] == "metadata_incomplete"
        and record["manifest"]["valid"] is False
        for record in report["backups"]
    )


def test_backup_audit_rejects_terminal_receipt_with_mismatched_generation(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    backup = backup_root / "raw-vault-projection-mismatched-terminal"
    backup.mkdir(parents=True)
    plan_hash = "1" * 64
    plan = {
        "schema_version": "mnemos.raw_projection_change_set.v1",
        "status": "planned",
        "plan_hash": plan_hash,
        "generation_hash": "2" * 64,
        "backup_dir": str(backup.resolve()),
        "changed_paths": ["codex/day/chunk.md"],
        "stale_paths": [],
        "index_changed_paths": ["codex/day/chunk.md"],
        "index_deleted_paths": [],
    }
    commit = {
        **plan,
        "status": "committed",
        "generation_hash": "3" * 64,
        "bytes_written": 1,
    }
    (backup / f"raw-projection-plan-{plan_hash}.json").write_text(
        json.dumps(plan),
        encoding="utf-8",
    )
    (backup / f"raw-projection-commit-{plan_hash}.json").write_text(
        json.dumps(commit),
        encoding="utf-8",
    )

    report = audit_raw_projection_backups(backup_root, max_total_bytes=1024 * 1024)

    record = report["backups"][0]
    assert record["recovery_class"] == "metadata_incomplete"
    assert record["manifest"]["valid"] is False
