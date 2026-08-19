"""
amphora 单元测试

覆盖项：
- enqueue / list_pending / get_next / mark_done / mark_failed / cleanup_old
- 原子性：get_next 后状态变为 processing
- 重试机制：mark_failed 后回到 pending，超过 max_retries 后 archived
- 优先级排序
- 进度更新
"""

import ast
from dataclasses import replace
import json
import subprocess
import sys
import unittest
import tempfile
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


from core.kia import amphora
from core.ops.durable_io import (
    DurableIOError,
    private_sqlite_sidecars,
    validate_private_sqlite_copy,
)


def test_terminal_owner_modules_import_without_amphora_cycle():
    for module in (
        "core.kia.amphora_cli",
        "core.kia.amphora_terminal_contract",
        "core.kia.amphora_terminal_operations",
        "core.kia.amphora_provenance_reconciliation",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    cli_path = (
        Path(__file__).resolve().parents[2] / "core/kia/amphora_cli.py"
    )
    cli_tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "core.kia.amphora"
        for node in ast.walk(cli_tree)
    )


class TestAmphoraQueue(unittest.TestCase):
    def setUp(self):
        # 使用临时目录隔离测试
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = amphora._DB_PATH
        amphora._DB_PATH = Path(self.tmpdir.name) / "test_distill_queue.db"

    def tearDown(self):
        amphora._DB_PATH = self._orig_db_path
        self.tmpdir.cleanup()

    def _commit_terminal_outbox_fixture(self, task_id):
        """Establish the independently tested terminal-proof precondition."""
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0]
            )
            outbox = meta["terminal_receipt_outbox"]
            self.assertEqual(outbox["status"], "pending")
            outbox.update(
                {
                    "status": "committed",
                    "committed_at": datetime.now().isoformat(),
                    "runtime_receipt_id": f"runtime-{task_id}",
                    "production_event_id": f"production-{task_id}",
                    "generation_id": f"generation-{task_id}",
                }
            )
            amphora._validated_terminal_receipt_outbox(
                outbox,
                task_id=task_id,
            )
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                (
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    task_id,
                ),
            )

    def test_enqueue_and_list_pending(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-1", [{"role": "user", "content": "hello"}], {"source": "test"}
        )
        self.assertEqual(receipt.session_id, "sess-1")

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["session_id"], "sess-1")
        self.assertIn("task_id", pending[0])
        self.assertTrue(Path(pending[0]["messages_path"]).exists())
        self.assertEqual(pending[0]["messages"][0]["content"], "hello")
        self.assertIsNotNone(pending[0]["created_at"])
        self.assertEqual(pending[0]["updated_at"], pending[0]["created_at"])

    def test_message_payload_publish_is_immutable_for_one_task_identity(self):
        first = amphora._write_messages(  # noqa: SLF001
            "fixed-task",
            [{"role": "user", "content": "first"}],
        )
        first_bytes = first.read_bytes()

        with self.assertRaisesRegex(
            DurableIOError,
            "durable_immutable_collision",
        ):
            amphora._write_messages(  # noqa: SLF001
                "fixed-task",
                [{"role": "user", "content": "second"}],
            )

        self.assertEqual(first.read_bytes(), first_bytes)

    def test_get_next_atomic(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])
        amphora.enqueue_with_receipt(
            "sess-2", [{"role": "user", "content": "b"}], priority=1
        )

        # Current enum value 1 is higher than the inferred normal priority 0.
        task = amphora.get_next()
        self.assertIsNotNone(task)
        self.assertEqual(task["session_id"], "sess-2")
        self.assertEqual(task["status"], "processing")
        self.assertEqual(task["progress_step"], amphora.DistillProgress.EXTRACTING.value)
        self.assertEqual(task["updated_at"], task["started_at"])

        # 再次 get_next 不应取到同一个
        task2 = amphora.get_next()
        self.assertIsNotNone(task2)
        self.assertEqual(task2["session_id"], "sess-1")

    def test_claim_task_claims_only_requested_session_atomically(self):
        first = amphora.enqueue_with_receipt(
            "sess-first",
            [{"role": "user", "content": "first"}],
        )
        amphora.enqueue_with_receipt(
            "sess-second",
            [{"role": "user", "content": "second"}],
        )

        claimed = amphora.claim_task(first.task_id)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], first.task_id)
        self.assertEqual(claimed["status"], "processing")
        self.assertTrue(claimed["started_at"])
        pending = amphora.list_pending()
        self.assertEqual([task["session_id"] for task in pending], ["sess-second"])

    def test_mark_done(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])
        amphora.get_next()

        ok = amphora.mark_done("sess-1", "/path/to/output.md")
        self.assertTrue(ok)

        # 再次 list_pending 应为空
        self.assertEqual(len(amphora.list_pending()), 0)

    def test_mark_failed_with_retry(self):
        amphora.enqueue_with_receipt(
            "sess-1", [{"role": "user", "content": "a"}], max_retries=2
        )
        amphora.get_next()

        # 第一次失败 → 回到 pending，但 next_retry_at 未到时不会被消费
        ok = amphora.mark_failed("sess-1", "timeout")
        self.assertTrue(ok)

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("retry 1/2", pending[0]["error"])
        self.assertIsNotNone(pending[0]["next_retry_at"])
        self.assertIsNone(amphora.get_next())

        # 手动把 retry 时间拨到过去，模拟 chronos 到点
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET next_retry_at = ? WHERE session_id = ?",
                ((datetime.now() - timedelta(minutes=1)).isoformat(), "sess-1"),
            )

        # 第二次失败 → archived
        amphora.get_next()
        amphora.mark_failed("sess-1", "timeout again")

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 0)
        self.assertEqual(amphora.get_task_count("failed"), 1)

    def test_stale_failure_cannot_overwrite_intentional_skip(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-stale-failure",
            [{"role": "user", "content": "a"}],
            max_retries=1,
        )
        claimed = amphora.get_next()
        self.assertIsNotNone(claimed)
        self.assertTrue(
            amphora.mark_terminal(
                receipt.task_id,
                amphora.DistillationWriteReceipt(
                    status="intentional_skip",
                    terminal_reason="reviewed no-effect",
                ),
                expected_started_at=claimed["started_at"],
            )
        )

        self.assertIsNone(
            amphora.mark_failed_with_transition(
                receipt.task_id,
                "late worker failure",
                expected_started_at=claimed["started_at"],
            )
        )
        task = amphora.list_tasks(status="intentional_skip", limit=1)[0]
        self.assertEqual(task["task_id"], receipt.task_id)
        self.assertNotIn("failed_terminal_receipt_outbox", task["meta"])

    def test_stale_success_cannot_overwrite_failed_terminal(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-stale-success",
            [{"role": "user", "content": "a"}],
            max_retries=1,
        )
        claimed = amphora.get_next()
        self.assertIsNotNone(claimed)
        transition = amphora.mark_failed_with_transition(
            receipt.task_id,
            "terminal failure",
            expected_started_at=claimed["started_at"],
        )
        self.assertIsNotNone(transition)
        self.assertTrue(transition.terminal)

        self.assertFalse(
            amphora.mark_terminal(
                receipt.task_id,
                amphora.DistillationWriteReceipt(
                    status="committed",
                    terminal_reason="late committed result",
                    written_pages=("/tmp/late.md",),
                    expected_count=1,
                    written_count=1,
                ),
                expected_started_at=claimed["started_at"],
            )
        )
        task = amphora.list_tasks(status="failed", limit=1)[0]
        self.assertEqual(task["task_id"], receipt.task_id)
        self.assertEqual(
            task["meta"]["failed_terminal_receipt_outbox"]["status"],
            "pending",
        )

    def test_retryable_receipt_exhaustion_creates_terminal_outbox(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-receipt-exhaustion",
            [{"role": "user", "content": "a"}],
            max_retries=1,
        )
        claimed = amphora.get_next()
        self.assertIsNotNone(claimed)

        self.assertTrue(
            amphora.mark_terminal(
                receipt.task_id,
                amphora.DistillationWriteReceipt(
                    status="retryable_failed",
                    terminal_reason="proposal write failed",
                    expected_count=1,
                    failed_count=1,
                ),
                expected_started_at=claimed["started_at"],
            )
        )

        failed = amphora.list_tasks(status="failed", limit=1)[0]
        self.assertEqual(failed["task_id"], receipt.task_id)
        outbox = failed["meta"]["failed_terminal_receipt_outbox"]
        self.assertEqual(outbox["status"], "pending")
        self.assertEqual(outbox["retry_count"], 1)
        self.assertEqual(outbox["max_retries"], 1)

    def test_reclaimed_attempt_rejects_previous_started_at_token(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-reclaimed-attempt",
            [{"role": "user", "content": "a"}],
        )
        first_claim = amphora.get_next()
        self.assertIsNotNone(first_claim)
        first_token = (
            datetime.now() - timedelta(hours=2)
        ).isoformat()
        first_claim["started_at"] = first_token
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                UPDATE distillation_tasks
                SET updated_at=?, started_at=?
                WHERE task_id=?
                """,
                (
                    first_token,
                    first_token,
                    receipt.task_id,
                ),
            )
        self.assertEqual(amphora.reset_timeouts(timeout_minutes=1), 1)
        second_claim = amphora.get_next()
        self.assertIsNotNone(second_claim)
        self.assertNotEqual(
            first_claim["started_at"],
            second_claim["started_at"],
        )

        self.assertIsNone(
            amphora.mark_failed_with_transition(
                receipt.task_id,
                "stale first attempt",
                expected_started_at=first_claim["started_at"],
            )
        )
        current = amphora.mark_failed_with_transition(
            receipt.task_id,
            "current second attempt",
            expected_started_at=second_claim["started_at"],
        )
        self.assertIsNotNone(current)

    def test_retry_failed_rejects_terminal_generation_reuse(self):
        amphora.enqueue_with_receipt(
            "sess-1", [{"role": "user", "content": "a"}], max_retries=1
        )
        amphora.get_next()
        amphora.mark_failed("sess-1", "api error")
        self.assertEqual(amphora.get_task_count("failed"), 1)

        with self.assertRaisesRegex(
            RuntimeError,
            "failed_terminal_generation_requires_new_input_revision",
        ):
            amphora.retry_failed("sess-1", reason="operator retry")

        self.assertEqual(amphora.get_task_count("failed"), 1)
        self.assertEqual(amphora.list_pending(), [])

    def test_retry_failed_rejects_legacy_terminal_without_receipt(self):
        amphora.enqueue_with_receipt(
            "sess-legacy", [{"role": "user", "content": "a"}], max_retries=1
        )
        amphora.get_next()
        amphora.mark_failed("sess-legacy", "api error")
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE session_id=?",
                    ("sess-legacy",),
                ).fetchone()[0]
            )
            meta.pop("failed_terminal_receipt_outbox")
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE session_id=?",
                (json.dumps(meta), "sess-legacy"),
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "failed_terminal_receipt_required_before_retry",
        ):
            amphora.retry_failed("sess-legacy", reason="legacy recovery")
        self.assertEqual(amphora.get_task_count("failed"), 1)

    def test_archive_failed_rejects_self_declared_committed_meta(self):
        amphora.enqueue_with_receipt(
            "sess-1", [{"role": "user", "content": "a"}], max_retries=1
        )
        amphora.get_next()
        amphora.mark_failed("sess-1", "api error")
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE session_id=?",
                    ("sess-1",),
                ).fetchone()[0]
            )
            outbox = meta["failed_terminal_receipt_outbox"]
            outbox["status"] = "committed"
            outbox["committed_at"] = datetime.now().isoformat()
            outbox["runtime_receipt_id"] = "forged-receipt"
            outbox["production_event_id"] = "forged-production"
            outbox["generation_id"] = "forged-generation"
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE session_id=?",
                (json.dumps(meta, sort_keys=True), "sess-1"),
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "failed_terminal_archive_receipt_verification_failed",
        ):
            amphora.archive_failed(
                reason="known transient historical failure",
                config={"database_dir": amphora._DB_PATH.parent},
            )
        self.assertEqual(amphora.get_task_count("failed"), 1)

    def test_enqueue_rejects_reserved_failed_terminal_outbox_meta(self):
        with self.assertRaisesRegex(
            ValueError,
            "failed_terminal_receipt_outbox_is_reserved",
        ):
            amphora.enqueue_with_receipt(
                "sess-forged",
                [{"role": "user", "content": "a"}],
                meta={
                    "failed_terminal_receipt_outbox": {
                        "task_id": "forged",
                        "status": "pending",
                    }
                },
            )

    def test_enqueue_rejects_reserved_success_terminal_outbox_meta(self):
        with self.assertRaisesRegex(
            ValueError,
            "terminal_receipt_outbox_is_reserved",
        ):
            amphora.enqueue_with_receipt(
                "sess-forged-success",
                [{"role": "user", "content": "a"}],
                meta={
                    "terminal_receipt_outbox": {
                        "task_id": "forged",
                        "status": "pending",
                    }
                },
            )

    def test_enqueue_rejects_reserved_message_cleanup_outbox_meta(self):
        with self.assertRaisesRegex(
            ValueError,
            "message_cleanup_outbox_is_reserved",
        ):
            amphora.enqueue_with_receipt(
                "sess-forged-cleanup",
                [{"role": "user", "content": "a"}],
                meta={
                    "message_cleanup_outbox": {
                        "task_id": "forged",
                        "status": "pending",
                    }
                },
            )

    def test_terminal_transition_rejects_corrupt_reserved_outbox(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-corrupt-outbox",
            [{"role": "user", "content": "a"}],
            max_retries=1,
        )
        amphora.get_next()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                (
                    json.dumps(
                        {
                            "failed_terminal_receipt_outbox": {
                                "task_id": receipt.task_id,
                                "status": "pending",
                            }
                        }
                    ),
                    receipt.task_id,
                ),
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "amphora_failed_terminal_outbox_invalid",
        ):
            amphora.mark_failed(receipt.task_id, "terminal error")
        task = amphora.list_tasks(limit=1)[0]
        self.assertEqual(task["status"], "processing")
        self.assertEqual(task["retry_count"], 0)

    def test_success_terminal_transition_rejects_corrupt_reserved_outbox(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-corrupt-success-outbox",
            [{"role": "user", "content": "a"}],
        )
        claimed = amphora.get_next()
        self.assertIsNotNone(claimed)
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                (
                    json.dumps(
                        {
                            "terminal_receipt_outbox": {
                                "task_id": receipt.task_id,
                                "status": "pending",
                            }
                        }
                    ),
                    receipt.task_id,
                ),
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "amphora_terminal_receipt_outbox_invalid",
        ):
            amphora.mark_terminal(
                receipt.task_id,
                amphora.DistillationWriteReceipt(
                    status="intentional_skip",
                    terminal_reason="no durable knowledge",
                ),
                expected_started_at=claimed["started_at"],
            )
        task = amphora.list_tasks(limit=1)[0]
        self.assertEqual(task["status"], "processing")

    def test_success_terminal_identity_poison_does_not_starve_valid_pending(self):
        poisoned = amphora.enqueue_with_receipt(
            "sess-success-identity-poison",
            [{"role": "user", "content": "poison"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(poisoned.task_id, "no knowledge")
        valid = amphora.enqueue_with_receipt(
            "sess-success-identity-valid",
            [{"role": "user", "content": "valid"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(valid.task_id, "no knowledge")
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET input_revision=? WHERE task_id=?",
                ("drifted", poisoned.task_id),
            )

        pending = amphora.list_terminal_receipt_outbox(limit=1)

        self.assertEqual(
            [item["task"]["task_id"] for item in pending],
            [valid.task_id],
        )
        poisoned_task = next(
            task
            for task in amphora.list_tasks(
                status="intentional_skip",
                limit=10,
            )
            if task["task_id"] == poisoned.task_id
        )
        self.assertTrue(
            poisoned_task["progress_detail"].startswith(
                "terminal_outbox_quarantined:"
            )
        )

    def test_archive_requires_pending_terminal_receipt_to_close(self):
        amphora.enqueue_with_receipt(
            "sess-pending-terminal",
            [{"role": "user", "content": "a"}],
            max_retries=1,
        )
        amphora.get_next()
        amphora.mark_failed("sess-pending-terminal", "api error")

        with self.assertRaisesRegex(
            RuntimeError,
            "failed_terminal_receipt_must_commit_before_archive",
        ):
            amphora.archive_failed(
                "sess-pending-terminal",
                reason="operator archive",
            )
        self.assertEqual(amphora.get_task_count("failed"), 1)

    def test_terminal_outbox_limit_counts_pending_not_older_committed_rows(self):
        pending_task_id = ""
        for index in range(101):
            receipt = amphora.enqueue_with_receipt(
                f"sess-terminal-{index:03d}",
                [{"role": "user", "content": str(index)}],
                max_retries=1,
            )
            amphora.get_next()
            amphora.mark_failed(receipt.task_id, "api error")
            if index < 100:
                with sqlite3.connect(str(amphora._DB_PATH)) as conn:
                    meta = json.loads(
                        conn.execute(
                            "SELECT meta FROM distillation_tasks WHERE task_id=?",
                            (receipt.task_id,),
                        ).fetchone()[0]
                    )
                    outbox = meta["failed_terminal_receipt_outbox"]
                    outbox["status"] = "committed"
                    outbox["committed_at"] = datetime.now().isoformat()
                    outbox["runtime_receipt_id"] = f"receipt-{index}"
                    outbox["production_event_id"] = f"production-{index}"
                    outbox["generation_id"] = f"generation-{index}"
                    conn.execute(
                        "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                        (
                            json.dumps(meta, sort_keys=True),
                            receipt.task_id,
                        ),
                    )
            else:
                pending_task_id = receipt.task_id

        pending = amphora.list_failed_terminal_receipt_outbox(limit=100)

        self.assertEqual(
            [item["task"]["task_id"] for item in pending],
            [pending_task_id],
        )

    def test_malformed_outbox_is_quarantined_without_starving_valid_pending(self):
        malformed = amphora.enqueue_with_receipt(
            "sess-malformed-first",
            [{"role": "user", "content": "first"}],
            max_retries=1,
        )
        amphora.get_next()
        amphora.mark_failed(malformed.task_id, "first failure")
        valid = amphora.enqueue_with_receipt(
            "sess-valid-second",
            [{"role": "user", "content": "second"}],
            max_retries=1,
        )
        amphora.get_next()
        amphora.mark_failed(valid.task_id, "second failure")
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE task_id=?",
                    (malformed.task_id,),
                ).fetchone()[0]
            )
            original_outbox = dict(
                meta["failed_terminal_receipt_outbox"]
            )
            meta["failed_terminal_receipt_outbox"] = {
                "task_id": malformed.task_id,
                "status": "pending",
            }
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                (json.dumps(meta), malformed.task_id),
            )

        pending = amphora.list_failed_terminal_receipt_outbox(limit=1)

        self.assertEqual(
            [item["task"]["task_id"] for item in pending],
            [valid.task_id],
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            quarantine = conn.execute(
                "SELECT progress_detail FROM distillation_tasks WHERE task_id=?",
                (malformed.task_id,),
            ).fetchone()[0]
        self.assertTrue(
            quarantine.startswith("failed_terminal_outbox_quarantined:")
        )

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            restored_meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE task_id=?",
                    (malformed.task_id,),
                ).fetchone()[0]
            )
            restored_meta["failed_terminal_receipt_outbox"] = original_outbox
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                (
                    json.dumps(restored_meta, sort_keys=True),
                    malformed.task_id,
                ),
            )

        recovered = amphora.list_failed_terminal_receipt_outbox(
            identifier=malformed.task_id,
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["task"]["progress_detail"], "")

    def test_failed_terminal_identity_poison_does_not_starve_valid_pending(self):
        poisoned = amphora.enqueue_with_receipt(
            "sess-failed-identity-poison",
            [{"role": "user", "content": "poison"}],
            max_retries=1,
        )
        amphora.get_next()
        amphora.mark_failed(poisoned.task_id, "failure")
        valid = amphora.enqueue_with_receipt(
            "sess-failed-identity-valid",
            [{"role": "user", "content": "valid"}],
            max_retries=1,
        )
        amphora.get_next()
        amphora.mark_failed(valid.task_id, "failure")
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET input_revision=? WHERE task_id=?",
                ("drifted", poisoned.task_id),
            )

        pending = amphora.list_failed_terminal_receipt_outbox(limit=1)

        self.assertEqual(
            [item["task"]["task_id"] for item in pending],
            [valid.task_id],
        )
        poisoned_task = next(
            task
            for task in amphora.list_tasks(status="failed", limit=10)
            if task["task_id"] == poisoned.task_id
        )
        self.assertTrue(
            poisoned_task["progress_detail"].startswith(
                "failed_terminal_outbox_quarantined:"
            )
        )

    def test_archive_rejects_legacy_failed_without_terminal_receipt(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-legacy-archive",
            [{"role": "user", "content": "a"}],
            max_retries=1,
        )
        amphora.get_next()
        amphora.mark_failed(receipt.task_id, "api error")
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE task_id=?",
                    (receipt.task_id,),
                ).fetchone()[0]
            )
            meta.pop("failed_terminal_receipt_outbox")
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                (json.dumps(meta), receipt.task_id),
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "failed_terminal_receipt_required_before_archive",
        ):
            amphora.archive_failed(receipt.task_id, reason="operator archive")
        self.assertEqual(amphora.get_task_count("failed"), 1)

    def test_get_next_prefers_fresh_pending_over_due_retry(self):
        amphora.enqueue_with_receipt("sess-retry", [{"role": "user", "content": "old"}])
        task = amphora.get_next()
        self.assertEqual(task["session_id"], "sess-retry")
        amphora.mark_failed("sess-retry", "timeout")

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET next_retry_at = ? WHERE session_id = ?",
                ((datetime.now() - timedelta(minutes=1)).isoformat(), "sess-retry"),
            )

        amphora.enqueue_with_receipt("sess-fresh", [{"role": "user", "content": "new"}])

        task = amphora.get_next()
        self.assertIsNotNone(task)
        self.assertEqual(task["session_id"], "sess-fresh")

    def test_cleanup_old(self):
        import time

        receipt = amphora.enqueue_with_receipt(
            "sess-old",
            [{"role": "user", "content": "a"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip("sess-old", "test cleanup terminal")
        self._commit_terminal_outbox_fixture(receipt.task_id)
        # 确保 completed_at 严格早于 cutoff（Windows 时间精度较低）
        time.sleep(0.05)

        pending_before = amphora.get_task_count()
        archived = amphora.cleanup_old(days=0)
        self.assertGreaterEqual(archived, 1)
        self.assertEqual(amphora.get_task_count(), pending_before)
        self.assertEqual(amphora.get_task_count("archived"), 1)
        archived_task = amphora.list_tasks(status="archived", limit=1)[0]
        first_updated_at = archived_task["updated_at"]
        self.assertEqual(amphora.cleanup_old(days=0), 0)
        self.assertEqual(
            amphora.list_tasks(status="archived", limit=1)[0][
                "updated_at"
            ],
            first_updated_at,
        )

    def test_cleanup_old_never_archives_failed_terminal_outbox(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-failed-cleanup",
            [{"role": "user", "content": "a"}],
            max_retries=1,
        )
        amphora.get_next()
        amphora.mark_failed(receipt.task_id, "api error")

        self.assertEqual(amphora.cleanup_old(days=0), 0)

        task = amphora.list_tasks(status="failed", limit=1)[0]
        self.assertEqual(task["task_id"], receipt.task_id)
        self.assertIsNotNone(task["messages_path"])
        self.assertEqual(
            task["meta"]["failed_terminal_receipt_outbox"]["status"],
            "pending",
        )
        self.assertEqual(
            len(
                amphora.list_failed_terminal_receipt_outbox(
                    identifier=receipt.task_id,
                )
            ),
            1,
        )

    def test_cleanup_old_waits_for_success_terminal_outbox(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-success-cleanup-pending",
            [{"role": "user", "content": "a"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(
            receipt.task_id,
            "no durable knowledge",
        )

        self.assertEqual(amphora.cleanup_old(days=0), 0)
        task = amphora.list_tasks(status="intentional_skip", limit=1)[0]
        self.assertEqual(task["task_id"], receipt.task_id)
        self.assertIsNotNone(task["messages_path"])
        self.assertEqual(
            task["meta"]["terminal_receipt_outbox"]["status"],
            "pending",
        )

    def test_cleanup_old_rejects_terminal_outbox_identity_drift(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-success-cleanup-identity-drift",
            [{"role": "user", "content": "a"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(
            receipt.task_id,
            "no durable knowledge",
        )
        self._commit_terminal_outbox_fixture(receipt.task_id)
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET input_revision=? WHERE task_id=?",
                ("drifted-revision", receipt.task_id),
            )

        self.assertEqual(amphora.cleanup_old(days=0), 0)
        task = amphora.list_tasks(status="intentional_skip", limit=1)[0]
        self.assertEqual(task["task_id"], receipt.task_id)
        self.assertIsNotNone(task["messages_path"])
        self.assertTrue(Path(task["messages_path"]).exists())
        self.assertTrue(
            task["progress_detail"].startswith(
                "message_cleanup_quarantined:"
            )
        )

    def test_cleanup_old_never_unlinks_uninspectable_message_path(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-cleanup-uninspectable",
            [{"role": "user", "content": "preserve"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(
            receipt.task_id,
            "no durable knowledge",
        )
        self._commit_terminal_outbox_fixture(receipt.task_id)
        task = amphora.list_tasks(status="intentional_skip", limit=1)[0]
        message_path = Path(task["messages_path"])
        before = message_path.read_bytes()
        original_lstat = Path.lstat

        def denied(path, *args, **kwargs):
            if path == message_path:
                raise PermissionError("sentinel")
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", denied):
            self.assertEqual(amphora.cleanup_old(days=0), 0)

        pending = amphora.list_tasks(
            status="intentional_skip",
            limit=1,
        )[0]
        self.assertEqual(pending["task_id"], receipt.task_id)
        self.assertEqual(message_path.read_bytes(), before)
        self.assertTrue(
            pending["progress_detail"].startswith(
                "message_cleanup_quarantined:"
            )
        )

    def test_cleanup_old_rejects_message_payload_revision_drift(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-cleanup-revision-drift",
            [{"role": "user", "content": "owned"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(
            receipt.task_id,
            "no durable knowledge",
        )
        self._commit_terminal_outbox_fixture(receipt.task_id)
        task = amphora.list_tasks(status="intentional_skip", limit=1)[0]
        message_path = Path(task["messages_path"])
        message_path.unlink()
        message_path.write_text(
            '[{"role":"user","content":"replacement"}]',
            encoding="utf-8",
        )

        self.assertEqual(amphora.cleanup_old(days=0), 0)

        pending = amphora.list_tasks(
            status="intentional_skip",
            limit=1,
        )[0]
        self.assertEqual(pending["task_id"], receipt.task_id)
        self.assertEqual(
            json.loads(message_path.read_text(encoding="utf-8"))[0]["content"],
            "replacement",
        )
        self.assertIn(
            "amphora_cleanup_messages_revision_mismatch",
            pending["progress_detail"],
        )

    def test_messages_directory_symlink_is_never_followed(self):
        messages_dir = Path(self.tmpdir.name) / "distill_messages"
        outside = Path(self.tmpdir.name) / "outside"
        outside.mkdir()
        messages_dir.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            RuntimeError,
            "amphora_messages_directory_unsafe",
        ):
            amphora._messages_dir()

        self.assertEqual(list(outside.iterdir()), [])

    def test_messages_directory_lookup_does_not_create_storage(self):
        messages_dir = Path(self.tmpdir.name) / "distill_messages"

        self.assertEqual(amphora._messages_dir(), messages_dir)  # noqa: SLF001
        self.assertFalse(messages_dir.exists())

    def test_cleanup_old_retries_file_reclaim_without_dangling_refs(self):
        receipts = []
        for index in range(2):
            receipt = amphora.enqueue_with_receipt(
                f"sess-cleanup-{index}",
                [{"role": "user", "content": str(index)}],
            )
            amphora.get_next()
            amphora.mark_intentional_skip(
                receipt.task_id,
                "no durable knowledge",
            )
            self._commit_terminal_outbox_fixture(receipt.task_id)
            receipts.append(receipt)
        paths = [
            Path(task["messages_path"])
            for task in amphora.list_tasks(
                status="intentional_skip",
                limit=10,
            )
        ]
        original_remove = amphora.secure_remove_regular_file
        calls = []

        def _flaky_remove(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise OSError("injected second reclaim failure")
            return original_remove(*args, **kwargs)

        with patch.object(
            amphora,
            "secure_remove_regular_file",
            _flaky_remove,
        ):
            self.assertEqual(amphora.cleanup_old(days=0), 1)

        archived = amphora.list_tasks(status="archived", limit=10)
        pending_cleanup = amphora.list_tasks(
            status="intentional_skip",
            limit=10,
        )
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(pending_cleanup), 1)
        self.assertIsNotNone(pending_cleanup[0]["messages_path"])
        self.assertEqual(
            pending_cleanup[0]["meta"]["message_cleanup_outbox"]["status"],
            "pending",
        )
        self.assertTrue(Path(pending_cleanup[0]["messages_path"]).exists())

        self.assertEqual(amphora.cleanup_old(days=0), 1)
        archived = amphora.list_tasks(status="archived", limit=10)
        self.assertEqual(
            {task["task_id"] for task in archived},
            {receipt.task_id for receipt in receipts},
        )
        self.assertTrue(all(task["messages_path"] is None for task in archived))
        self.assertTrue(
            all(
                task["meta"]["message_cleanup_outbox"]["status"]
                == "committed"
                for task in archived
            )
        )
        self.assertEqual(sum(path.exists() for path in paths), 0)
        self.assertEqual(amphora.cleanup_old(days=0), 0)

    def test_cleanup_old_quarantines_invalid_path_without_starving_valid(self):
        poisoned = amphora.enqueue_with_receipt(
            "sess-cleanup-poisoned",
            [{"role": "user", "content": "poisoned"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(
            poisoned.task_id,
            "no durable knowledge",
        )
        self._commit_terminal_outbox_fixture(poisoned.task_id)
        valid = amphora.enqueue_with_receipt(
            "sess-cleanup-valid",
            [{"role": "user", "content": "valid"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(valid.task_id, "no durable knowledge")
        self._commit_terminal_outbox_fixture(valid.task_id)
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                UPDATE distillation_tasks
                SET messages_path=?
                WHERE task_id=?
                """,
                (
                    str(Path(self.tmpdir.name) / "outside.json"),
                    poisoned.task_id,
                ),
            )

        self.assertEqual(amphora.cleanup_old(days=0), 1)
        valid_task = amphora.list_tasks(
            status="archived",
            limit=10,
        )
        self.assertEqual(
            [task["task_id"] for task in valid_task],
            [valid.task_id],
        )
        poisoned_task = amphora.list_tasks(
            status="intentional_skip",
            limit=10,
        )[0]
        self.assertEqual(poisoned_task["task_id"], poisoned.task_id)
        self.assertTrue(
            poisoned_task["progress_detail"].startswith(
                "message_cleanup_quarantined:"
            )
        )

    def test_cleanup_old_retries_after_message_directory_fsync_failure(self):
        receipt = amphora.enqueue_with_receipt(
            "sess-cleanup-fsync",
            [{"role": "user", "content": "fsync"}],
        )
        amphora.get_next()
        amphora.mark_intentional_skip(
            receipt.task_id,
            "no durable knowledge",
        )
        self._commit_terminal_outbox_fixture(receipt.task_id)

        with patch(
            "core.kia.amphora.fsync_directory",
            side_effect=OSError("injected directory fsync failure"),
        ):
            self.assertEqual(amphora.cleanup_old(days=0), 0)

        pending = amphora.list_tasks(
            status="intentional_skip",
            limit=1,
        )[0]
        self.assertEqual(pending["task_id"], receipt.task_id)
        self.assertIsNotNone(pending["messages_path"])
        self.assertFalse(Path(pending["messages_path"]).exists())
        self.assertEqual(
            pending["meta"]["message_cleanup_outbox"]["status"],
            "pending",
        )

        self.assertEqual(amphora.cleanup_old(days=0), 1)
        archived = amphora.list_tasks(status="archived", limit=1)[0]
        self.assertEqual(archived["task_id"], receipt.task_id)
        self.assertIsNone(archived["messages_path"])
        self.assertEqual(
            archived["meta"]["message_cleanup_outbox"]["status"],
            "committed",
        )

    def test_progress_update(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])
        stale_updated_at = (datetime.now() - timedelta(minutes=10)).isoformat()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET updated_at = ? WHERE session_id = ?",
                (stale_updated_at, "sess-1"),
            )
        self.assertTrue(
            amphora.update_progress(
                "sess-1", amphora.DistillProgress.VERIFYING.value, "quality check"
            )
        )

        pending = amphora.list_pending()
        self.assertEqual(pending[0]["progress_step"], amphora.DistillProgress.VERIFYING.value)
        self.assertEqual(pending[0]["progress_detail"], "quality check")
        self.assertGreater(pending[0]["updated_at"], stale_updated_at)

    def test_structuring_progress_is_public_queue_stage(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])

        updated = amphora.update_progress(
            "sess-1", amphora.DistillProgress.STRUCTURING.value, "building structure"
        )

        self.assertTrue(updated)
        pending = amphora.list_pending()
        self.assertEqual(pending[0]["progress_step"], amphora.DistillProgress.STRUCTURING.value)
        self.assertEqual(pending[0]["progress_detail"], "building structure")

    def test_writing_progress_is_public_queue_stage(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])

        updated = amphora.update_progress(
            "sess-1", amphora.DistillProgress.WRITING.value, "writing wiki page"
        )

        self.assertTrue(updated)
        pending = amphora.list_pending()
        self.assertEqual(pending[0]["progress_step"], amphora.DistillProgress.WRITING.value)
        self.assertEqual(pending[0]["progress_detail"], "writing wiki page")

    def test_priority_ordering(self):
        amphora.enqueue_with_receipt(
            "sess-low",
            [{"role": "user", "content": "low"}],
            priority=amphora.TaskPriority.NORMAL.value,
        )
        amphora.enqueue_with_receipt(
            "sess-high", [{"role": "user", "content": "high"}], priority=1
        )

        task = amphora.get_next()
        self.assertEqual(task["session_id"], "sess-high")

    def test_meta_infers_priority(self):
        amphora.enqueue_with_receipt(
            "sess-normal", [{"role": "user", "content": "normal"}]
        )
        amphora.enqueue_with_receipt(
            "sess-urgent", [{"role": "user", "content": "urgent"}], {"urgent": True}
        )

        task = amphora.get_next()
        self.assertEqual(task["session_id"], "sess-urgent")

    def test_mark_done_accepts_task_id(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])
        task = amphora.get_next()

        self.assertTrue(amphora.mark_done(task["task_id"], "/path/to/output.md"))
        self.assertEqual(amphora.get_task_count("done"), 1)

    def test_late_revision_creates_new_task_without_overwriting_first(self):
        amphora.enqueue_with_receipt(
            "sess-1", [{"role": "user", "content": "original"}]
        )
        first = amphora.list_pending()[0]

        amphora.enqueue_with_receipt(
            "sess-1", [{"role": "user", "content": "replacement"}]
        )
        pending = amphora.list_pending()
        second = next(task for task in pending if task["input_revision"] != first["input_revision"])

        self.assertNotEqual(first["messages_path"], second["messages_path"])
        self.assertEqual(first["messages"][0]["content"], "original")
        self.assertEqual(second["messages"][0]["content"], "replacement")
        self.assertEqual(amphora.get_task_count(), 2)

    def test_exact_duplicate_returns_same_receipt(self):
        messages = [{"role": "user", "content": "same"}]
        first = amphora.enqueue_with_receipt("sess-1", messages, {"source": "codex"})
        second = amphora.enqueue_with_receipt("sess-1", messages, {"source": "codex"})

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(amphora.get_task_count(), 1)

    def test_same_declared_input_revision_rejects_different_messages(self):
        first_messages = [{"role": "user", "content": "first"}]
        receipt = amphora.enqueue_with_receipt(
            "sess-bound",
            first_messages,
            {"source": "codex", "input_revision": "raw-generation-1"},
        )
        task = amphora.list_pending()[0]
        message_path = Path(task["messages_path"])
        first_bytes = message_path.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            "amphora_existing_task_payload_identity_mismatch",
        ):
            amphora.enqueue_with_receipt(
                "sess-bound",
                [{"role": "user", "content": "second"}],
                {"source": "codex", "input_revision": "raw-generation-1"},
            )

        self.assertEqual(amphora.get_task_count(), 1)
        self.assertEqual(amphora.list_pending()[0]["task_id"], receipt.task_id)
        self.assertEqual(message_path.read_bytes(), first_bytes)

    def test_enqueue_rejects_invalid_retry_budget_before_payload_publish(self):
        for invalid in (0, -1, True, 1.5):
            with self.assertRaisesRegex(
                ValueError,
                "max_retries_must_be_positive_integer",
            ):
                amphora.enqueue_with_receipt(
                    "sess-invalid-retry",
                    [{"role": "user", "content": "never queued"}],
                    max_retries=invalid,
                )

        self.assertEqual(amphora.get_task_count(), 0)
        messages_dir = Path(self.tmpdir.name) / "distill_messages"
        self.assertFalse(messages_dir.exists())

    def test_get_next_never_follows_symlinked_message_payload(self):
        amphora.enqueue_with_receipt(
            "sess-message-link",
            [{"role": "user", "content": "owned"}],
        )
        task = amphora.list_pending()[0]
        message_path = Path(task["messages_path"])
        sentinel = Path(self.tmpdir.name) / "foreign-message.json"
        sentinel.write_text(
            '[{"role":"user","content":"FOREIGN-SECRET"}]',
            encoding="utf-8",
        )
        message_path.unlink()
        message_path.symlink_to(sentinel)

        with self.assertRaisesRegex(
            amphora.AmphoraTaskPayloadUnavailableError,
            "amphora_task_messages_unreadable",
        ):
            amphora.get_next()

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM distillation_tasks WHERE task_id=?",
                    (task["task_id"],),
                ).fetchone(),
                ("pending",),
            )
        self.assertIn(b"FOREIGN-SECRET", sentinel.read_bytes())

    def test_same_session_from_different_agents_never_collides(self):
        messages = [{"role": "user", "content": "same"}]
        codex = amphora.enqueue_with_receipt("shared-session", messages, {"source": "codex"})
        claude = amphora.enqueue_with_receipt("shared-session", messages, {"source": "claude"})

        self.assertNotEqual(codex.task_id, claude.task_id)
        self.assertEqual(amphora.get_task_count(), 2)

    def test_session_unique_legacy_schema_migrates_to_revision_contract(self):
        amphora._DB_PATH.unlink(missing_ok=True)
        legacy_messages = [{"role": "user", "content": "legacy payload"}]
        messages_dir = Path(self.tmpdir.name) / "distill_messages"
        messages_dir.mkdir()
        messages_path = messages_dir / "legacy-task.json"
        messages_path.write_text(
            json.dumps(legacy_messages),
            encoding="utf-8",
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                CREATE TABLE distillation_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    messages_path TEXT,
                    meta TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO distillation_tasks (
                    task_id, session_id, messages_path, meta, created_at, updated_at
                ) VALUES ('legacy-task', 'legacy-session', ?, '{"source":"codex"}', ?, ?)
                """,
                (
                    str(messages_path),
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            )

        amphora._init_db()
        amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "late revision"}],
            {"source": "codex"},
        )

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='distillation_tasks'"
            ).fetchone()[0]
            count = conn.execute(
                "SELECT COUNT(*) FROM distillation_tasks WHERE session_id='legacy-session'"
            ).fetchone()[0]
            legacy_meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE task_id='legacy-task'"
                ).fetchone()[0]
            )
        self.assertNotIn("session_id TEXT UNIQUE", table_sql)
        self.assertEqual(count, 2)
        self.assertEqual(
            legacy_meta["messages_revision"],
            amphora._messages_revision(legacy_messages),
        )

    def test_add_missing_revision_columns_backfills_exact_messages_revision(self):
        amphora._DB_PATH.unlink(missing_ok=True)
        legacy_messages = [{"role": "user", "content": "revision-column payload"}]
        messages_dir = Path(self.tmpdir.name) / "distill_messages"
        messages_dir.mkdir()
        messages_path = messages_dir / "revision-column-task.json"
        messages_path.write_text(
            json.dumps(legacy_messages),
            encoding="utf-8",
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                CREATE TABLE distillation_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    messages_path TEXT,
                    meta TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    output_path TEXT,
                    error TEXT,
                    updated_at TEXT,
                    terminal_outbox_anchor_sha256 TEXT NOT NULL DEFAULT ''
                )
                """
            )
            amphora._create_terminal_outbox_anchor_trigger(conn)
            conn.execute(
                """
                INSERT INTO distillation_tasks (
                    task_id, session_id, messages_path, meta, created_at, updated_at
                ) VALUES (
                    'revision-column-task',
                    'revision-column-session',
                    ?,
                    '{"source":"codex"}',
                    ?,
                    ?
                )
                """,
                (
                    str(messages_path),
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            )

        amphora._init_db()

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            row = conn.execute(
                "SELECT source_agent, input_revision, generation, meta "
                "FROM distillation_tasks WHERE task_id='revision-column-task'"
            ).fetchone()
        expected_revision = amphora._messages_revision(legacy_messages)
        self.assertEqual(row[:3], ("codex", expected_revision, 1))
        meta = json.loads(row[3])
        self.assertEqual(meta["messages_revision"], expected_revision)
        self.assertEqual(
            amphora.SYSTEM_OWNED_META_KEYS.intersection(meta),
            {"messages_revision"},
        )

    def test_legacy_schema_migration_never_invents_empty_unreadable_messages(
        self,
    ):
        amphora._DB_PATH.unlink(missing_ok=True)
        messages_dir = Path(self.tmpdir.name) / "distill_messages"
        messages_dir.mkdir()
        messages_path = messages_dir / "legacy-messages.json"
        messages_path.write_text(
            '[{"role":"user","content":"preserve"}]',
            encoding="utf-8",
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                CREATE TABLE distillation_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    messages_path TEXT,
                    meta TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO distillation_tasks (
                    task_id, session_id, messages_path, meta, created_at, updated_at
                ) VALUES ('legacy-task', 'legacy-session', ?, '{"source":"codex"}', ?, ?)
                """,
                (
                    str(messages_path),
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            )
        original_secure_read_bytes = amphora.secure_read_bytes

        def denied(root, relative_path):
            if (Path(root) / Path(relative_path)).absolute() == messages_path.absolute():
                raise PermissionError("sentinel")
            return original_secure_read_bytes(root, relative_path)

        with patch.object(amphora, "secure_read_bytes", denied):
            with self.assertRaisesRegex(
                amphora.AmphoraTaskPayloadUnavailableError,
                "amphora_task_messages_unreadable",
            ):
                amphora._init_db()

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='distillation_tasks'"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT task_id, messages_path FROM distillation_tasks"
            ).fetchone()
        self.assertIn("session_id TEXT UNIQUE", table_sql)
        self.assertEqual(row, ("legacy-task", str(messages_path)))
        self.assertIn(b"preserve", messages_path.read_bytes())

    def test_schema_initialization_rolls_back_late_ddl_failure_as_one_unit(self):
        amphora._DB_PATH.unlink(missing_ok=True)

        with patch(
            "core.kia.amphora._create_source_span_migration_table",
            side_effect=RuntimeError("injected late schema failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected late schema failure",
            ):
                amphora._init_db()

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            objects = conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        self.assertEqual(objects, [])

    def test_existing_revision_schema_without_terminal_anchor_fails_closed(self):
        amphora._DB_PATH.unlink(missing_ok=True)
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                CREATE TABLE distillation_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source_agent TEXT NOT NULL DEFAULT 'unknown',
                    input_revision TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL
                )
                """
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "canonical_terminal_outbox_anchor_upgrade_required",
        ):
            amphora._init_db()

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(distillation_tasks)")
            }
        self.assertNotIn("terminal_outbox_anchor_sha256", columns)

    def test_existing_terminal_anchor_without_immutable_trigger_fails_closed(self):
        amphora._init_db()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "DROP TRIGGER distillation_tasks_terminal_outbox_anchor_immutable"
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "canonical_terminal_outbox_anchor_upgrade_required",
        ):
            amphora._init_db()

    def test_same_named_noop_terminal_anchor_trigger_fails_closed(self):
        amphora._init_db()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "DROP TRIGGER distillation_tasks_terminal_outbox_anchor_immutable"
            )
            conn.execute(
                """
                CREATE TRIGGER distillation_tasks_terminal_outbox_anchor_immutable
                BEFORE UPDATE OF terminal_outbox_anchor_sha256
                ON distillation_tasks
                BEGIN
                    SELECT 1;
                END
                """
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "canonical_terminal_outbox_anchor_upgrade_required",
        ):
            amphora._init_db()

    def test_enqueue_never_rebinds_legacy_provenance_without_reconciliation(self):
        messages = [{"role": "user", "content": "same visible text"}]
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            messages,
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET status='reconciliation_required', "
                "terminal_reason='legacy_done_without_typed_terminal_receipt' "
                "WHERE task_id=?",
                (legacy.task_id,),
            )

        current = amphora.enqueue_with_receipt(
            "legacy-session",
            messages,
            {
                "source": "codex",
                "input_revision": "canonical-raw-revision",
                "handoff_receipt_id": "handoff-current",
            },
        )

        self.assertTrue(current.created)
        self.assertNotEqual(current.task_id, legacy.task_id)
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            legacy_row = conn.execute(
                "SELECT input_revision, handoff_receipt_id, terminal_reason "
                "FROM distillation_tasks WHERE task_id=?",
                (legacy.task_id,),
            ).fetchone()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM distillation_tasks").fetchone()[0],
                2,
            )
        self.assertEqual(
            legacy_row,
            (
                "legacy-revision",
                "",
                "legacy_done_without_typed_terminal_receipt",
            ),
        )

    def _migrated_legacy_provenance_fixture(self):
        visible = [{"role": "user", "content": "same visible text"}]
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            visible,
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET status='pending', "
                "terminal_reason='legacy_done_without_typed_terminal_receipt' "
                "WHERE task_id=?",
                (legacy.task_id,),
            )
        canonical_messages = [
            {
                "role": "user",
                "content": "same visible text",
                "turn": 0,
                "turn_number": 0,
                "source_span": {
                    "revision_id": "rawrev-current",
                    "content_hash": "sha256:current",
                    "role": "user",
                    "span_start": 0,
                    "span_end": 17,
                },
            }
        ]
        canonical_revision = amphora._messages_revision(canonical_messages)
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        migrated = amphora.reconcile_historical_task_provenance(
            session_id="legacy-session",
            messages=canonical_messages,
            meta={
                "source": "codex",
                "input_revision": canonical_revision,
                "handoff_receipt_id": "handoff-current",
            },
            reviewed_task_id=legacy.task_id,
            expected_old_input_revision="legacy-revision",
            expected_object_hash=reviewed["object_hash"],
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=Path(self.tmpdir.name) / "backup",
        )
        return legacy, migrated, canonical_revision

    def test_provenance_inventory_never_follows_symlinked_message_asset(self):
        legacy = amphora.enqueue_with_receipt(
            "legacy-symlink-session",
            [{"role": "user", "content": "owned legacy bytes"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        task = amphora.list_pending()[0]
        messages_path = Path(task["messages_path"])
        sentinel = Path(self.tmpdir.name) / "foreign-legacy-messages.json"
        sentinel.write_bytes(messages_path.read_bytes())
        messages_path.unlink()
        messages_path.symlink_to(sentinel)

        with self.assertRaisesRegex(
            ValueError,
            "provenance messages asset (?:is outside owner|is unsafe)",
        ):
            amphora.build_historical_provenance_inventory()

        self.assertIn(b"owned legacy bytes", sentinel.read_bytes())

    def test_provenance_inventory_rejects_stale_messages_revision_metadata(self):
        legacy, migrated, canonical_revision = (
            self._migrated_legacy_provenance_fixture()
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE task_id=?",
                    (migrated.task_id,),
                ).fetchone()[0]
            )
            meta["messages_revision"] = "forged-stale-revision"
            conn.execute(
                "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
                (json.dumps(meta, ensure_ascii=False), migrated.task_id),
            )

        inventory = amphora.build_historical_provenance_inventory()

        reviewed = next(
            item for item in inventory["objects"]
            if item["primary_key"] == legacy.task_id
        )
        self.assertEqual(canonical_revision, meta["input_revision"])
        self.assertFalse(reviewed["covered"])
        self.assertEqual(
            reviewed["coverage_error"],
            "canonical_task_binding_mismatch",
        )

    def test_explicit_migration_preserves_legacy_and_creates_reviewed_object(self):
        visible = [{"role": "user", "content": "same visible text"}]
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            visible,
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET status='pending', "
                "terminal_reason='legacy_done_without_typed_terminal_receipt' "
                "WHERE task_id=?",
                (legacy.task_id,),
            )
        canonical_messages = [
            {
                "role": "user",
                "content": "same visible text",
                "turn": 0,
                "turn_number": 0,
                "source_span": {
                    "revision_id": "rawrev-current",
                    "content_hash": "sha256:current",
                    "role": "user",
                    "span_start": 0,
                    "span_end": 17,
                },
            }
        ]
        canonical_revision = amphora._messages_revision(canonical_messages)

        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        backup_dir = Path(self.tmpdir.name) / "backup"
        migrated = amphora.reconcile_historical_task_provenance(
            session_id="legacy-session",
            messages=canonical_messages,
            meta={
                "source": "codex",
                "input_revision": canonical_revision,
                "handoff_receipt_id": "handoff-current",
            },
            reviewed_task_id=legacy.task_id,
            expected_old_input_revision="legacy-revision",
            expected_object_hash=reviewed["object_hash"],
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=backup_dir,
        )

        self.assertIsNotNone(migrated)
        self.assertNotEqual(migrated.task_id, legacy.task_id)
        self.assertTrue(migrated.created)
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            legacy_row = conn.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (legacy.task_id,),
            ).fetchone()
            canonical_row = conn.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (migrated.task_id,),
            ).fetchone()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM distillation_tasks").fetchone()[0],
                2,
            )
            migration = conn.execute(
                "SELECT * FROM amphora_provenance_migrations"
            ).fetchone()
        self.assertEqual(legacy_row["input_revision"], "legacy-revision")
        self.assertEqual(legacy_row["handoff_receipt_id"], "")
        self.assertEqual(
            legacy_row["terminal_reason"],
            "legacy_done_without_typed_terminal_receipt",
        )
        self.assertEqual(canonical_row["input_revision"], canonical_revision)
        self.assertEqual(canonical_row["handoff_receipt_id"], "handoff-current")
        canonical_meta = json.loads(canonical_row["meta"])
        self.assertEqual(
            canonical_meta["messages_revision"],
            canonical_revision,
        )
        self.assertEqual(
            amphora.SYSTEM_OWNED_META_KEYS.intersection(canonical_meta),
            {"messages_revision"},
        )
        self.assertEqual(migration["legacy_task_id"], legacy.task_id)
        self.assertEqual(migration["legacy_object_hash"], reviewed["object_hash"])
        self.assertEqual(
            canonical_meta["provenance_migration"]["schema_version"],
            "mnemos.amphora_provenance_migration.v2",
        )
        self.assertEqual(
            json.loads(Path(canonical_row["messages_path"]).read_text(encoding="utf-8")),
            canonical_messages,
        )
        backup_manifest = backup_dir / legacy.task_id / "backup_manifest.json"
        self.assertTrue(backup_manifest.is_file())
        database_backup = backup_dir / legacy.task_id / "distill_queue.db"
        self.assertTrue(database_backup.is_file())
        self.assertEqual(database_backup.stat().st_mode & 0o777, 0o600)
        self.assertFalse(
            any(path.exists() for path in private_sqlite_sidecars(database_backup))
        )
        validate_private_sqlite_copy(database_backup)
        with sqlite3.connect(str(database_backup)) as backup_connection:
            self.assertEqual(
                backup_connection.execute("PRAGMA journal_mode").fetchone(),
                ("delete",),
            )
        replay = amphora.reconcile_historical_task_provenance(
            session_id="legacy-session",
            messages=canonical_messages,
            meta={
                "source": "codex",
                "input_revision": canonical_revision,
                "handoff_receipt_id": "handoff-current",
            },
            reviewed_task_id=legacy.task_id,
            expected_old_input_revision="legacy-revision",
            expected_object_hash=reviewed["object_hash"],
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=backup_dir,
        )
        self.assertFalse(replay.created)
        self.assertEqual(replay.task_id, migrated.task_id)

    def test_legacy_migration_rejects_unreviewed_or_drifted_object(self):
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET progress_detail='drift' WHERE task_id=?",
                (legacy.task_id,),
            )

        with self.assertRaisesRegex(ValueError, "inventory hash drifted"):
            amphora.reconcile_historical_task_provenance(
                session_id="legacy-session",
                messages=[{"role": "user", "content": "legacy"}],
                meta={
                    "source": "codex",
                    "input_revision": "canonical-revision",
                    "handoff_receipt_id": "handoff-current",
                },
                reviewed_task_id=legacy.task_id,
                expected_old_input_revision="legacy-revision",
                expected_object_hash=reviewed["object_hash"],
                expected_inventory_hash=inventory["inventory_hash"],
                backup_dir=Path(self.tmpdir.name) / "backup",
            )

    def test_legacy_migration_cleans_messages_when_receipt_insert_fails(self):
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
            conn.execute(
                """
                CREATE TRIGGER reject_provenance_migration
                BEFORE INSERT ON amphora_provenance_migrations
                BEGIN
                    SELECT RAISE(ABORT, 'injected migration receipt failure');
                END
                """
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        canonical_revision = amphora._messages_revision(
            [{"role": "user", "content": "legacy"}]
        )
        canonical_task_id = amphora._task_id(
            "legacy-session", "codex", canonical_revision
        )
        canonical_messages = (
            amphora._messages_dir() / f"{canonical_task_id}.json"
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected migration receipt failure"
        ):
            amphora.reconcile_historical_task_provenance(
                session_id="legacy-session",
                messages=[{"role": "user", "content": "legacy"}],
                meta={
                    "source": "codex",
                    "input_revision": canonical_revision,
                    "handoff_receipt_id": "handoff-current",
                },
                reviewed_task_id=legacy.task_id,
                expected_old_input_revision="legacy-revision",
                expected_object_hash=reviewed["object_hash"],
                expected_inventory_hash=inventory["inventory_hash"],
                backup_dir=Path(self.tmpdir.name) / "backup",
            )

        self.assertFalse(canonical_messages.exists())
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM distillation_tasks WHERE task_id=?",
                    (canonical_task_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM amphora_provenance_migrations"
                ).fetchone()[0],
                0,
            )
            conn.execute("DROP TRIGGER reject_provenance_migration")

        replay_after_failure = amphora.reconcile_historical_task_provenance(
            session_id="legacy-session",
            messages=[{"role": "user", "content": "legacy"}],
            meta={
                "source": "codex",
                "input_revision": canonical_revision,
                "handoff_receipt_id": "handoff-current",
            },
            reviewed_task_id=legacy.task_id,
            expected_old_input_revision="legacy-revision",
            expected_object_hash=reviewed["object_hash"],
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=Path(self.tmpdir.name) / "backup",
        )
        self.assertTrue(replay_after_failure.created)

    def test_legacy_migration_never_unlinks_replaced_message_on_rollback(self):
        from core.kia import amphora_provenance_reconciliation

        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
            conn.execute(
                """
                CREATE TRIGGER reject_provenance_migration_replacement
                BEFORE INSERT ON amphora_provenance_migrations
                BEGIN
                    SELECT RAISE(ABORT, 'injected migration receipt failure');
                END
                """
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        canonical_messages = [{"role": "user", "content": "legacy"}]
        canonical_revision = amphora._messages_revision(canonical_messages)
        canonical_task_id = amphora._task_id(
            "legacy-session",
            "codex",
            canonical_revision,
        )
        canonical_path = amphora._messages_dir() / f"{canonical_task_id}.json"
        runtime = amphora_provenance_reconciliation._runtime()  # noqa: SLF001
        original_write = runtime.write_messages

        def replace_after_publish(task_id, messages, **kwargs):
            publication = original_write(task_id, messages, **kwargs)
            path = getattr(publication, "path", publication)
            path.unlink()
            path.write_bytes(b"foreign-replacement")
            return publication

        patched_runtime = replace(runtime, write_messages=replace_after_publish)
        with patch.object(
            amphora_provenance_reconciliation,
            "_RUNTIME",
            patched_runtime,
        ):
            with self.assertRaisesRegex(
                DurableIOError,
                "durable_target_preimage_changed",
            ):
                amphora.reconcile_historical_task_provenance(
                    session_id="legacy-session",
                    messages=canonical_messages,
                    meta={
                        "source": "codex",
                        "input_revision": canonical_revision,
                        "handoff_receipt_id": "handoff-current",
                    },
                    reviewed_task_id=legacy.task_id,
                    expected_old_input_revision="legacy-revision",
                    expected_object_hash=reviewed["object_hash"],
                    expected_inventory_hash=inventory["inventory_hash"],
                    backup_dir=Path(self.tmpdir.name) / "backup",
                )

        self.assertEqual(canonical_path.read_bytes(), b"foreign-replacement")

    def test_legacy_migration_rejects_replaced_message_before_commit(self):
        from core.kia import amphora_provenance_reconciliation

        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        canonical_messages = [{"role": "user", "content": "legacy"}]
        canonical_revision = amphora._messages_revision(canonical_messages)
        canonical_task_id = amphora._task_id(
            "legacy-session",
            "codex",
            canonical_revision,
        )
        canonical_path = amphora._messages_dir() / f"{canonical_task_id}.json"
        runtime = amphora_provenance_reconciliation._runtime()  # noqa: SLF001
        original_write = runtime.write_messages

        def replace_after_publish(task_id, messages, **kwargs):
            publication = original_write(task_id, messages, **kwargs)
            path = getattr(publication, "path", publication)
            path.unlink()
            path.write_bytes(b"foreign-before-commit")
            return publication

        patched_runtime = replace(runtime, write_messages=replace_after_publish)
        with patch.object(
            amphora_provenance_reconciliation,
            "_RUNTIME",
            patched_runtime,
        ):
            with self.assertRaisesRegex(
                DurableIOError,
                "durable_target_preimage_changed",
            ):
                amphora.reconcile_historical_task_provenance(
                    session_id="legacy-session",
                    messages=canonical_messages,
                    meta={
                        "source": "codex",
                        "input_revision": canonical_revision,
                        "handoff_receipt_id": "handoff-current",
                    },
                    reviewed_task_id=legacy.task_id,
                    expected_old_input_revision="legacy-revision",
                    expected_object_hash=reviewed["object_hash"],
                    expected_inventory_hash=inventory["inventory_hash"],
                    backup_dir=Path(self.tmpdir.name) / "backup",
                )

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            assert conn.execute(
                "SELECT 1 FROM distillation_tasks WHERE task_id=?",
                (canonical_task_id,),
            ).fetchone() is None
            assert conn.execute(
                "SELECT 1 FROM amphora_provenance_migrations "
                "WHERE canonical_task_id=?",
                (canonical_task_id,),
            ).fetchone() is None
        self.assertEqual(canonical_path.read_bytes(), b"foreign-before-commit")

    def test_legacy_migration_rejects_messages_drift_during_backup(self):
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        from core.kia import amphora_provenance_support

        original_read = (
            amphora_provenance_support.read_owned_message_asset_bytes
        )
        provenance_reads = 0

        def drift_before_backup_read(**kwargs):
            nonlocal provenance_reads
            if kwargs.get("purpose") == "provenance messages asset":
                provenance_reads += 1
                if provenance_reads == 2:
                    Path(kwargs["messages_path"]).write_text(
                        "[]",
                        encoding="utf-8",
                    )
            return original_read(**kwargs)

        canonical_messages = [{"role": "user", "content": "legacy"}]
        with patch(
            "core.kia.amphora_provenance_support.read_owned_message_asset_bytes",
            side_effect=drift_before_backup_read,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "messages asset drifted",
            ):
                amphora.reconcile_historical_task_provenance(
                    session_id="legacy-session",
                    messages=canonical_messages,
                    meta={
                        "source": "codex",
                        "input_revision": amphora._messages_revision(
                            canonical_messages
                        ),
                        "handoff_receipt_id": "handoff-current",
                    },
                    reviewed_task_id=legacy.task_id,
                    expected_old_input_revision="legacy-revision",
                    expected_object_hash=reviewed["object_hash"],
                    expected_inventory_hash=inventory["inventory_hash"],
                    backup_dir=Path(self.tmpdir.name) / "backup",
                )

        backup_root = Path(self.tmpdir.name) / "backup"
        self.assertTrue(backup_root.is_dir())
        self.assertEqual(list(backup_root.iterdir()), [])

    def test_legacy_migration_rejects_symlinked_backup_root(self):
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        external = Path(self.tmpdir.name) / "foreign-backup-root"
        external.mkdir()
        backup_root = Path(self.tmpdir.name) / "backup-link"
        backup_root.symlink_to(external, target_is_directory=True)
        canonical_messages = [{"role": "user", "content": "legacy"}]

        with self.assertRaisesRegex(ValueError, "backup root is unsafe"):
            amphora.reconcile_historical_task_provenance(
                session_id="legacy-session",
                messages=canonical_messages,
                meta={
                    "source": "codex",
                    "input_revision": amphora._messages_revision(
                        canonical_messages
                    ),
                    "handoff_receipt_id": "handoff-current",
                },
                reviewed_task_id=legacy.task_id,
                expected_old_input_revision="legacy-revision",
                expected_object_hash=reviewed["object_hash"],
                expected_inventory_hash=inventory["inventory_hash"],
                backup_dir=backup_root,
            )

        self.assertEqual(list(external.iterdir()), [])

    def test_legacy_migration_removes_partial_backup_generation_on_publish_failure(
        self,
    ):
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        backup_root = Path(self.tmpdir.name) / "backup"
        canonical_messages = [{"role": "user", "content": "legacy"}]

        with patch(
            "core.kia.amphora_provenance_support.secure_publish_immutable_bytes",
            side_effect=OSError("injected messages backup publish failure"),
        ):
            with self.assertRaisesRegex(
                OSError,
                "injected messages backup publish failure",
            ):
                amphora.reconcile_historical_task_provenance(
                    session_id="legacy-session",
                    messages=canonical_messages,
                    meta={
                        "source": "codex",
                        "input_revision": amphora._messages_revision(
                            canonical_messages
                        ),
                        "handoff_receipt_id": "handoff-current",
                    },
                    reviewed_task_id=legacy.task_id,
                    expected_old_input_revision="legacy-revision",
                    expected_object_hash=reviewed["object_hash"],
                    expected_inventory_hash=inventory["inventory_hash"],
                    backup_dir=backup_root,
                )

        self.assertTrue(backup_root.is_dir())
        self.assertEqual(list(backup_root.iterdir()), [])

    def test_provenance_inventory_never_follows_symlinked_backup_asset(self):
        legacy, _migrated, _canonical_revision = (
            self._migrated_legacy_provenance_fixture()
        )
        messages_backup = (
            Path(self.tmpdir.name)
            / "backup"
            / legacy.task_id
            / "messages.json"
        )
        sentinel = Path(self.tmpdir.name) / "foreign-backup-messages.json"
        sentinel.write_bytes(messages_backup.read_bytes())
        messages_backup.unlink()
        messages_backup.symlink_to(sentinel)

        inventory = amphora.build_historical_provenance_inventory()

        reviewed = next(
            item
            for item in inventory["objects"]
            if item["primary_key"] == legacy.task_id
        )
        self.assertFalse(reviewed["covered"])
        self.assertEqual(
            reviewed["coverage_error"],
            "backup_receipt_binding_mismatch",
        )
        self.assertIn(b"same visible text", sentinel.read_bytes())

    def test_legacy_migration_rejects_different_visible_messages(self):
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy A"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]

        with self.assertRaisesRegex(ValueError, "visible messages differ"):
            amphora.reconcile_historical_task_provenance(
                session_id="legacy-session",
                messages=[{"role": "user", "content": "DIFFERENT B"}],
                meta={
                    "source": "codex",
                    "input_revision": "canonical-revision",
                    "handoff_receipt_id": "handoff-current",
                },
                reviewed_task_id=legacy.task_id,
                expected_old_input_revision="legacy-revision",
                expected_object_hash=reviewed["object_hash"],
                expected_inventory_hash=inventory["inventory_hash"],
                backup_dir=Path(self.tmpdir.name) / "backup",
            )

    def test_inventory_marks_orphaned_migration_receipt_uncovered(self):
        legacy = amphora.enqueue_with_receipt(
            "legacy-session",
            [{"role": "user", "content": "legacy"}],
            {"source": "codex", "input_revision": "legacy-revision"},
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
                ("legacy_done_without_typed_terminal_receipt", legacy.task_id),
            )
        inventory = amphora.build_historical_provenance_inventory()
        reviewed = inventory["objects"][0]
        canonical_messages = [{"role": "user", "content": "legacy"}]
        canonical_revision = amphora._messages_revision(canonical_messages)
        migrated = amphora.reconcile_historical_task_provenance(
            session_id="legacy-session",
            messages=canonical_messages,
            meta={
                "source": "codex",
                "input_revision": canonical_revision,
                "handoff_receipt_id": "handoff-current",
            },
            reviewed_task_id=legacy.task_id,
            expected_old_input_revision="legacy-revision",
            expected_object_hash=reviewed["object_hash"],
            expected_inventory_hash=inventory["inventory_hash"],
            backup_dir=Path(self.tmpdir.name) / "backup",
        )
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "DELETE FROM distillation_tasks WHERE task_id=?",
                (migrated.task_id,),
            )

        after = amphora.build_historical_provenance_inventory()
        self.assertEqual(after["uncovered_count"], 1)
        self.assertFalse(after["objects"][0]["covered"])
        self.assertEqual(
            after["objects"][0]["coverage_error"], "canonical_task_missing"
        )

    def test_reset_timeouts_returns_stuck_processing_to_pending(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])
        amphora.get_next()

        stale_started_at = (datetime.now() - timedelta(minutes=31)).isoformat()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                UPDATE distillation_tasks
                SET started_at = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (stale_started_at, stale_started_at, "sess-1"),
            )

        reset_count = amphora.reset_timeouts(timeout_minutes=30)

        self.assertEqual(reset_count, 1)
        pending = amphora.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0]["started_at"])
        self.assertEqual(pending[0]["progress_step"], amphora.DistillProgress.PENDING.value)
        self.assertIn("processing timeout", pending[0]["error"])
        self.assertEqual(pending[0]["progress_detail"], "reset by timeout watchdog")
        self.assertGreater(pending[0]["updated_at"], stale_started_at)

    def test_reset_timeouts_uses_latest_processing_activity_timestamp(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])
        amphora.get_next()

        stale_updated_at = (datetime.now() - timedelta(minutes=31)).isoformat()
        fresh_started_at = datetime.now().isoformat()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                UPDATE distillation_tasks
                SET started_at = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (fresh_started_at, stale_updated_at, "sess-1"),
            )

        reset_count = amphora.reset_timeouts(timeout_minutes=30)

        self.assertEqual(reset_count, 0)
        self.assertEqual(amphora.get_task_count("processing"), 1)

    def test_reset_timeouts_excludes_only_the_exact_live_claim(self):
        first = amphora.enqueue_with_receipt(
            "sess-live",
            [{"role": "user", "content": "live"}],
        )
        second = amphora.enqueue_with_receipt(
            "sess-stale",
            [{"role": "user", "content": "stale"}],
        )
        live_claim = amphora.claim_task(first.task_id)
        stale_claim = amphora.claim_task(second.task_id)
        self.assertIsNotNone(live_claim)
        self.assertIsNotNone(stale_claim)
        stale_at = (datetime.now() - timedelta(hours=2)).isoformat()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                """
                UPDATE distillation_tasks
                SET started_at=?, updated_at=?
                WHERE task_id IN (?, ?)
                """,
                (stale_at, stale_at, first.task_id, second.task_id),
            )

        reset_count = amphora.reset_timeouts(
            timeout_minutes=30,
            excluded_claims=((first.task_id, stale_at),),
        )

        self.assertEqual(reset_count, 1)
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            rows = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT task_id, status, started_at FROM distillation_tasks"
                )
            }
        self.assertEqual(rows[first.task_id], ("processing", stale_at))
        self.assertEqual(rows[second.task_id], ("pending", None))

    def test_missing_message_file_fails_closed_before_claim(self):
        amphora.enqueue_with_receipt("sess-1", [{"role": "user", "content": "a"}])
        pending = amphora.list_pending()[0]
        Path(pending["messages_path"]).unlink()

        with self.assertRaisesRegex(
            amphora.AmphoraTaskPayloadUnavailableError,
            "amphora_task_messages_file_missing",
        ):
            amphora.get_next()

        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            status, started_at = conn.execute(
                "SELECT status, started_at FROM distillation_tasks WHERE session_id='sess-1'"
            ).fetchone()
        self.assertEqual(status, "pending")
        self.assertIsNone(started_at)


if __name__ == "__main__":
    unittest.main()
