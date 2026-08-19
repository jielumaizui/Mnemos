# -*- coding: utf-8 -*-
"""Unit tests for database retention / maintenance (P0 4.1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from core.db_utils import delete_older_than


class TestDeleteOlderThan:
    """通用过期行清理 helper 测试。"""

    def test_deletes_old_rows_and_keeps_recent(self, tmp_db_conn):
        conn = tmp_db_conn
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT)")
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn.executemany("INSERT INTO t (ts) VALUES (?)", [(old,), (recent,)])
        conn.commit()

        deleted = delete_older_than(conn, "t", "ts", days=30)
        assert deleted == 1
        remaining = conn.execute("SELECT ts FROM t").fetchall()
        assert len(remaining) == 1
        assert remaining[0][0] == recent

    def test_respects_batch_limit(self, tmp_db_conn, monkeypatch):
        conn = tmp_db_conn
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT)")
        old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.executemany("INSERT INTO t (ts) VALUES (?)", [(old,) for _ in range(5)])
        conn.commit()

        deleted = delete_older_than(conn, "t", "ts", days=30, limit=2)
        assert deleted == 5
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0

    def test_dry_run_counts_without_deleting(self, tmp_db_conn):
        conn = tmp_db_conn
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT)")
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        conn.executemany("INSERT INTO t (ts) VALUES (?)", [(old,) for _ in range(3)])
        conn.commit()

        count = delete_older_than(conn, "t", "ts", days=30, dry_run=True)
        assert count == 3
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3


class TestObservationStoreCleanup:
    """ObservationStore.cleanup_older_than 集成测试。"""

    def test_cleanup_older_than_deletes_stale_observations(self, tmp_path, patched_get_config):
        from core.cognitive.observation_store import ObservationStore

        db_path = tmp_path / "observations.db"
        store = ObservationStore(db_path=str(db_path))

        # 直接插入测试数据，避免构造完整 Observation 对象
        with sqlite3.connect(str(db_path)) as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
            recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            conn.executemany(
                """INSERT INTO observations (
                    id, dimension, observation_type, value, source_type, source_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("old-1", "focus", "type", "{}", "raw", "src", old, old),
                    ("recent-1", "focus", "type", "{}", "raw", "src", recent, recent),
                ],
            )
            conn.commit()

        deleted = store.cleanup_older_than(days=180)
        assert deleted == 1

        with sqlite3.connect(str(db_path)) as conn:
            ids = {row[0] for row in conn.execute("SELECT id FROM observations").fetchall()}
        assert ids == {"recent-1"}


class TestDatabaseMaintenanceTask:
    """DatabaseMaintenanceTask 维护任务测试。"""

    def test_dry_run_previews_deletions(self, tmp_path, patched_get_config, monkeypatch):
        from daemon.maintenance import DatabaseMaintenanceTask
        from core.telemetry.prompt_call_log import ModelCallLedger

        cfg = patched_get_config
        cfg._values["llm.provider_prices"] = {
            "test": {"model": {"input": 0.1, "output": 0.2}}
        }
        db_path = cfg.database_dir / "model_call_ledger.db"
        ledger = ModelCallLedger(db_path=db_path, config=cfg)
        ledger.start_run("recent", subject_scope=("source", "maintenance-test"))
        recent = ledger.reserve(
            run_id="recent", operation="distill_extract", provider="test", model="model",
            input_text="x", input_tokens=1,
        )
        recent.release()

        # Runtime ledger rows use canonical UTC ISO timestamps; a noncanonical
        # fixture belongs to the backup-gated reconciliation path, not retention.
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE model_call_entries SET created_at=?", (old,))
            conn.commit()

        task = DatabaseMaintenanceTask(config=cfg)
        result = task.run(dry_run=True, force=True)
        assert result["dry_run"] is True
        assert result["deleted"]["model_call_ledger"] == 1

        # 确认干跑未删除
        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0]
        assert count == 1

    def test_run_deletes_old_rows(self, tmp_path, patched_get_config):
        from daemon.maintenance import DatabaseMaintenanceTask

        cfg = patched_get_config
        cfg._values["llm.provider_prices"] = {
            "test": {"model": {"input": 0.1, "output": 0.2}}
        }
        cfg.database_dir.mkdir(parents=True, exist_ok=True)
        from core.telemetry.prompt_call_log import ModelCallLedger

        db_path = cfg.database_dir / "model_call_ledger.db"
        ledger = ModelCallLedger(db_path=db_path, config=cfg)
        ledger.start_run("old", subject_scope=("source", "maintenance-test"))
        reservation = ledger.reserve(
            run_id="old", operation="distill_extract", provider="test", model="model",
            input_text="x", input_tokens=1,
        )
        reservation.release()
        # Keep the retention fixture within the normal runtime data contract.
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE model_call_entries SET created_at=?", (old,))
            conn.commit()

        task = DatabaseMaintenanceTask(config=cfg)
        result = task.run(dry_run=False, force=True)
        assert result["deleted"]["model_call_ledger"] == 1

        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM model_call_entries").fetchone()[0]
        assert count == 0

    def test_run_deletes_old_distillation_chunk_checkpoints(
        self, tmp_path, patched_get_config
    ):
        from daemon.maintenance import DatabaseMaintenanceTask
        from core.hephaestus.chunk_checkpoint import (
            CheckpointAdmission,
            ChunkCheckpointStore,
            build_checkpoint_output_hash,
        )
        from core.hephaestus.distill_execution_spec import DistillExecutionSpec
        from core.hephaestus.distill_input_spec import (
            OUTPUT_CONTRACT_VERSION,
            DistillInputSpec,
        )

        cfg = patched_get_config
        db_path = cfg.database_dir / "distillation_chunks.db"
        store = ChunkCheckpointStore(db_path)
        input_spec = DistillInputSpec.build(
            source_agent="maintenance-test",
            source_session_id="old",
            source_event_ids=["raw-old"],
            raw_completeness="full",
            visible_input="expired checkpoint maintenance fixture",
            input_mode="chunked",
        )
        spec = DistillExecutionSpec(
            input_contract_version="lossless-visible-v1",
            input_spec_hash=input_spec.input_spec_hash,
            output_admission_contract_version=OUTPUT_CONTRACT_VERSION,
            prompt_version="prompt-v1",
            prompt_hash="sha256:prompt",
            output_schema_hash="sha256:schema",
            extractor_contract_hash="sha256:extractor",
            backend_hash="sha256:backend",
            merge_contract_hash="sha256:merge",
            model_ids=("provider/model",),
            config_values={},
        )
        structured_output = {
            "schema_version": OUTPUT_CONTRACT_VERSION,
            **input_spec.prompt_contract(),
            "distill_intent": "skip",
            "candidate_summary": "维护任务的过期检查点 fixture 不代表知识输出。",
            "skip_reason": "该 fixture 只用于验证保留期清理。",
            "no_value_evidence": [
                {
                    "source_event_id": input_spec.source_event_ids[0],
                    "reason": "维护 fixture 没有可沉淀的用户知识。",
                }
            ],
            "claims": [],
        }
        canonical_output = {
            "judgment": "skip",
            "judgment_reason": "仅验证过期 checkpoint 的维护清理。",
            "fragments": [],
            "structured_output": structured_output,
        }
        admission = CheckpointAdmission(
            input_spec_hash=input_spec.input_spec_hash,
            output_contract_version=OUTPUT_CONTRACT_VERSION,
            canonical_output_hash=build_checkpoint_output_hash(canonical_output),
            judgment="skip",
        )
        store.save_completed(
            "old",
            0,
            "hash-old",
            spec,
            [],
            {
                "input_spec_hash": admission.input_spec_hash,
                "output_contract_version": admission.output_contract_version,
                "canonical_output_hash": admission.canonical_output_hash,
                "output_judgment": admission.judgment,
            },
            structured_output,
            admission,
            canonical_output=canonical_output,
            input_spec=input_spec,
        )

        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                UPDATE distill_chunk_results
                SET updated_at = ?
                WHERE session_id = ?
                """,
                (old, "old"),
            )
            conn.commit()

        task = DatabaseMaintenanceTask(config=cfg)
        result = task.run(dry_run=False, force=True)

        assert result["deleted"]["distillation_chunks"] == 1
        assert store.load_completed("old", 0, "hash-old", spec) is None
