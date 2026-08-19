"""Tests for verified legacy Raw-projection backup cleanup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.reconcile_raw_projection_backups import (
    RawProjectionBackupReconcileError,
    apply_cleanup,
    plan_cleanup,
)


def _legacy_backup(root: Path, name: str, *, size: int = 16) -> Path:
    target = root / name
    target.mkdir(parents=True)
    (target / "legacy.md").write_bytes(b"x" * size)
    return target


def _canonical_preconditions() -> dict:
    return {
        "raw_integrity_check": "ok",
        "raw_foreign_key_violations": 0,
        "raw_turn_count": 1,
        "projection_journal_file_count": 1,
        "ok": True,
    }


def test_plan_and_apply_delete_only_audited_legacy_directories(tmp_path: Path):
    backup_root = tmp_path / "backups"
    legacy = _legacy_backup(backup_root, "raw-vault-projection-old", size=4096)
    metadata_only = backup_root / "raw-vault-projection-metadata"
    metadata_only.mkdir()
    (metadata_only / "raw-projection-change-manifest.json").write_text(
        json.dumps({"schema_version": "mnemos.raw_projection_change_set.v1"}),
        encoding="utf-8",
    )

    plan = plan_cleanup(backup_root, max_total_bytes=512)

    assert plan["status"] == "cleanup_required"
    assert [item["backup_id"] for item in plan["selected"]] == [legacy.name]

    result = apply_cleanup(
        backup_root,
        max_total_bytes=512,
        receipt_dir=tmp_path / "receipts",
        canonical_preconditions=_canonical_preconditions(),
    )

    assert result["deleted_backup_count"] == 1
    assert not legacy.exists()
    assert metadata_only.exists()
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["status"] == "committed"
    assert receipt["completed_backup_ids"] == ["raw-vault-projection-old"]


def test_cleanup_refuses_inventory_drift_before_deletion(tmp_path: Path):
    backup_root = tmp_path / "backups"
    legacy = _legacy_backup(backup_root, "raw-vault-projection-old", size=32)
    plan = plan_cleanup(backup_root, max_total_bytes=1)
    legacy.joinpath("later.md").write_bytes(b"changed")

    with pytest.raises(RawProjectionBackupReconcileError, match="inventory changed"):
        # Supply the stale plan by making the live plan appear eligible, then
        # exercise the per-directory invariant through a deterministic monkeypatch.
        from unittest.mock import patch

        with patch(
            "scripts.reconcile_raw_projection_backups.plan_cleanup", return_value=plan
        ):
            apply_cleanup(
                backup_root,
                max_total_bytes=1,
                receipt_dir=tmp_path / "receipts",
                canonical_preconditions=_canonical_preconditions(),
            )
    assert legacy.exists()
