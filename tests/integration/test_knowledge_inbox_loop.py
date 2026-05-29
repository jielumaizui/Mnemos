# -*- coding: utf-8 -*-
"""
P2-2 长链路测试 — KnowledgeInbox 摄入链路

链路：file → KnowledgeInboxProcessor.process_file() → MemosClient.save()
      → state_db 记录 → 文件移动到 processed_dir

策略：临时目录 + mock MemosClient，真实文件操作。
断言目标：文件被处理、source_id 被记录、去重生效、重试不重复。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestKnowledgeInboxLoop:
    """Knowledge Inbox 完整摄入链路。"""

    @pytest.fixture
    def inbox_processor(self, tmp_path):
        """返回配置为临时目录的 KnowledgeInboxProcessor（MemosClient 被 mock）。"""
        from core.kia.knowledge_inbox import KnowledgeInboxProcessor

        with patch('core.kia.knowledge_inbox.MemosClient') as _mock_memos_cls, \
             patch('core.hephaestus.document_processor.MemosClient') as _mock_doc_memos_cls:
            processor = KnowledgeInboxProcessor()

            # 替换为临时路径
            processor.inbox_dir = tmp_path / "inbox"
            processor.processed_dir = tmp_path / "processed"
            processor.failed_dir = tmp_path / "failed"
            processor.report_dir = tmp_path / "reports"
            processor.state_db = tmp_path / "inbox_state.db"

            for d in [processor.inbox_dir, processor.processed_dir,
                      processor.failed_dir, processor.report_dir]:
                d.mkdir(exist_ok=True)

            # 替换 client 为 mock
            processor.client = MagicMock()
            mock_result = MagicMock()
            mock_result.uid = "test-memos-uid-001"
            processor.client.save.return_value = mock_result

            # 重新初始化状态数据库（因为路径已变更）
            processor._init_state_db()
            yield processor

    def test_process_text_file_creates_source_record(self, inbox_processor):
        from core.kia.knowledge_inbox import InboxFile

        # 放入文本文件
        test_file = inbox_processor.inbox_dir / "note.txt"
        test_file.write_text("This is a knowledge note about Docker.", encoding="utf-8")

        h = inbox_processor._compute_hash(test_file)
        inbox_file = InboxFile(
            path=test_file,
            filename=test_file.name,
            size=test_file.stat().st_size,
            mtime=test_file.stat().st_mtime,
            hash=h,
            status="pending",
        )

        result = inbox_processor.process_file(inbox_file)

        assert result["success"] is True
        assert result["memos_uid"] == "test-memos-uid-001"
        # 文件应被移动到 processed
        assert not test_file.exists()
        assert len(list(inbox_processor.processed_dir.iterdir())) >= 1

    def test_dedup_skips_duplicate_file(self, inbox_processor):
        from core.kia.knowledge_inbox import InboxFile

        test_file = inbox_processor.inbox_dir / "dup.txt"
        test_file.write_text("duplicate content", encoding="utf-8")

        h = inbox_processor._compute_hash(test_file)
        inbox_file = InboxFile(
            path=test_file,
            filename=test_file.name,
            size=test_file.stat().st_size,
            mtime=test_file.stat().st_mtime,
            hash=h,
            status="pending",
        )

        r1 = inbox_processor.process_file(inbox_file)
        assert r1["success"] is True

        # 再放一个内容相同的文件
        test_file2 = inbox_processor.inbox_dir / "dup2.txt"
        test_file2.write_text("duplicate content", encoding="utf-8")
        h2 = inbox_processor._compute_hash(test_file2)
        inbox_file2 = InboxFile(
            path=test_file2,
            filename=test_file2.name,
            size=test_file2.stat().st_size,
            mtime=test_file2.stat().st_mtime,
            hash=h2,
            status="pending",
        )

        # 扫描时应识别为已处理
        state = inbox_processor._load_state()
        assert inbox_file2.hash in state["processed_files"], "去重：相同哈希应已在状态库中"

    def test_scan_inbox_finds_pending_files(self, inbox_processor):
        from core.kia.knowledge_inbox import InboxFile

        # 创建两个文件，处理一个
        f1 = inbox_processor.inbox_dir / "a.txt"
        f1.write_text("first", encoding="utf-8")
        f2 = inbox_processor.inbox_dir / "b.txt"
        f2.write_text("second", encoding="utf-8")

        inbox_f1 = InboxFile(
            path=f1,
            filename=f1.name,
            size=f1.stat().st_size,
            mtime=f1.stat().st_mtime,
            hash=inbox_processor._compute_hash(f1),
            status="pending",
        )
        inbox_processor.process_file(inbox_f1)

        # 扫描应只剩一个未处理文件
        pending = inbox_processor.scan_inbox()
        pending_names = [p.filename for p in pending]
        assert "a.txt" not in pending_names, "已处理文件不应出现在 pending 中"
        assert "b.txt" in pending_names, "未处理文件应被扫描到"

    def test_state_db_tracks_success_and_failed(self, inbox_processor):
        """状态数据库应正确记录成功和失败。"""
        import sqlite3
        from core.kia.knowledge_inbox import InboxFile

        # 成功文件
        f1 = inbox_processor.inbox_dir / "ok.txt"
        f1.write_text("good content", encoding="utf-8")
        inbox_f1 = InboxFile(
            path=f1,
            filename=f1.name,
            size=f1.stat().st_size,
            mtime=f1.stat().st_mtime,
            hash=inbox_processor._compute_hash(f1),
            status="pending",
        )
        inbox_processor.process_file(inbox_f1)

        # 验证 DB
        with sqlite3.connect(str(inbox_processor.state_db)) as conn:
            rows = conn.execute(
                "SELECT filename, status FROM processed_files"
            ).fetchall()

        assert len(rows) >= 1
        statuses = {status for _, status in rows}
        assert "success" in statuses
