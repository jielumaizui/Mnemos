# -*- coding: utf-8 -*-
"""Unit tests for core/db_utils.py"""

import sqlite3
import threading
import time

import pytest

from core.db_utils import (
    DEFAULT_BUSY_TIMEOUT_MS,
    render_sql,
    sqlite_conn,
    sqlite_artifact_exists,
    sqlite_artifact_size,
    SqlitePool,
    validate_sql_identifier,
)

# ---------------------------------------------------------------------------
# sqlite_conn
# ---------------------------------------------------------------------------


class TestSqliteConn:
    """sqlite_conn 上下文管理器测试"""

    def test_sqlite_conn_creates_db(self, tmp_path):
        """with 块应创建数据库文件。"""
        db_path = tmp_path / "test.db"
        with sqlite_conn(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        assert db_path.exists()

    def test_sqlite_conn_commits_on_success(self, tmp_path):
        """无异常时应提交事务。"""
        db_path = tmp_path / "test.db"
        with sqlite_conn(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")

        # 重新连接验证数据已提交
        with sqlite3.connect(str(db_path)) as conn2:
            row = conn2.execute("SELECT * FROM t").fetchone()
            assert row == (1,)

    def test_sqlite_conn_rollback_on_exception(self, tmp_path):
        """异常时应回滚事务。"""
        db_path = tmp_path / "test.db"
        with sqlite_conn(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")

        try:
            with sqlite_conn(str(db_path)) as conn:
                conn.execute("INSERT INTO t VALUES (2)")
                raise ValueError("boom")
        except ValueError:
            pass

        with sqlite3.connect(str(db_path)) as conn2:
            row = conn2.execute("SELECT * FROM t WHERE id=2").fetchone()
            assert row is None

    def test_sqlite_conn_closes_connection(self, tmp_path):
        """with 块退出后连接应关闭。"""
        db_path = tmp_path / "test.db"
        conn_ref = {"conn": None}
        with sqlite_conn(str(db_path)) as conn:
            conn_ref["conn"] = conn
            conn.execute("CREATE TABLE t (id INTEGER)")

        # sqlite3.Connection 没有公开的 closed 属性，但可以通过操作检测
        with pytest.raises(sqlite3.ProgrammingError):
            conn_ref["conn"].execute("SELECT 1")

    def test_sqlite_conn_passes_kwargs(self, tmp_path):
        """应传递 timeout 等参数给 sqlite3.connect。"""
        db_path = tmp_path / "test.db"
        with sqlite_conn(str(db_path), timeout=5) as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")
        assert db_path.exists()

    def test_sqlite_conn_configures_wal_and_busy_timeout(self, tmp_path):
        """默认连接应启用 WAL 和 busy_timeout，降低 daemon 并发锁冲突。"""
        db_path = tmp_path / "test.db"
        with sqlite_conn(str(db_path)) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        assert journal_mode.lower() == "wal"
        assert busy_timeout >= DEFAULT_BUSY_TIMEOUT_MS

    def test_sqlite_conn_retries_locked_writes(self, tmp_path, monkeypatch):
        """遇到 database locked 时应退避重试，而不是立即放弃。"""
        db_path = tmp_path / "test.db"
        with sqlite_conn(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")

        blocker = sqlite3.connect(str(db_path), timeout=0.01)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("INSERT INTO t VALUES (1)")

        sleeps = []
        monkeypatch.setattr("core.db_utils.time.sleep", lambda seconds: sleeps.append(seconds))
        try:
            with pytest.raises(sqlite3.OperationalError):
                with sqlite_conn(
                    str(db_path),
                    timeout=0.01,
                    busy_timeout_ms=10,
                    lock_retries=2,
                    lock_retry_base=0.01,
                ) as conn:
                    conn.execute("INSERT INTO t VALUES (2)")
        finally:
            blocker.rollback()
            blocker.close()

        assert sleeps == [0.01, 0.02]


# ---------------------------------------------------------------------------
# SqlitePool
# ---------------------------------------------------------------------------


class TestSqlitePool:
    """SqlitePool 持久连接池测试"""

    def test_pool_creates_db(self, tmp_path):
        """get_conn 应自动创建数据库文件。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)
        conn = pool.get_conn()
        conn.execute("CREATE TABLE t (id INTEGER)")
        pool.close()
        assert db_path.exists()

    def test_pool_reuses_connection(self, tmp_path):
        """同一线程应复用连接。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)
        conn1 = pool.get_conn()
        conn2 = pool.get_conn()
        assert conn1 is conn2
        pool.close()

    def test_pool_can_disable_persistent_connections(self, tmp_path):
        """非持久模式不缓存连接，适合短操作释放文件锁。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path, persistent=False)
        conn1 = pool.get_conn()
        conn2 = pool.get_conn()
        try:
            assert conn1 is not conn2
            assert pool._conns == {}
            assert pool._transient_conns == {conn1, conn2}
            conn1.execute("CREATE TABLE t (id INTEGER)")
            conn1.commit()
            conn2.execute("INSERT INTO t VALUES (1)")
            conn2.commit()
            pool.release_transient_connections()
            assert pool._transient_conns == set()
        finally:
            pool.close()

    def test_transient_pool_release_is_thread_local(self, tmp_path):
        """释放短连接只应影响当前线程，不能关闭其他线程正在使用的连接。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path, persistent=False)
        ready = threading.Event()
        proceed = threading.Event()
        errors = []

        def worker():
            conn = pool.get_conn()
            ready.set()
            proceed.wait(timeout=5)
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
            except Exception as exc:  # pragma: no cover - asserted via errors
                errors.append(exc)
            finally:
                pool.release_transient_connections()

        thread = threading.Thread(target=worker)
        thread.start()
        assert ready.wait(timeout=5)

        pool.release_transient_connections()
        proceed.set()
        thread.join(timeout=5)

        try:
            assert not thread.is_alive()
            assert errors == []
            with sqlite3.connect(str(db_path)) as conn:
                assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
        finally:
            pool.close()

    def test_sqlite_artifact_helpers_ignore_legacy_encrypted_artifacts(self, tmp_path):
        """SQLite artifact helpers only treat the canonical .db as the database."""

        db_path = tmp_path / "sync_log.db"
        db_path.with_suffix(".db.enc").write_text("cipher", encoding="utf-8")

        assert sqlite_artifact_exists(db_path) is False
        assert sqlite_artifact_size(db_path) == (0, False)

        db_path.write_bytes(b"db")

        assert sqlite_artifact_exists(db_path) is True
        assert sqlite_artifact_size(db_path) == (2, False)

    def test_pool_thread_isolation(self, tmp_path):
        """不同线程应有独立连接。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)
        conns = {}

        def worker(name):
            conns[name] = pool.get_conn()

        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert conns["t1"] is not conns["t2"]
        pool.close()

    def test_pool_context_manager(self, tmp_path):
        """上下文管理器应自动关闭所有连接。"""
        db_path = tmp_path / "pool.db"
        with SqlitePool(db_path) as pool:
            conn = pool.get_conn()
            conn.execute("CREATE TABLE t (id INTEGER)")
        # 退出后所有连接已关闭

    def test_pool_close_clears_connections(self, tmp_path):
        """close() 应清空连接字典。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)
        conn = pool.get_conn()
        conn.execute("CREATE TABLE t (id INTEGER)")
        pool.close()
        assert len(pool._conns) == 0

    def test_pool_rolls_back_hanging_transaction(self, tmp_path):
        """get_conn 应自动回滚挂起的事务。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)
        conn = pool.get_conn()
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t VALUES (1)")
        # 不提交，模拟异常退出

        # 再次 get_conn 应自动回滚
        conn2 = pool.get_conn()
        conn2.execute("INSERT INTO t VALUES (2)")
        conn2.commit()
        pool.close()

        # 验证只有 (2) 被提交
        with sqlite3.connect(str(db_path)) as verify:
            rows = verify.execute("SELECT * FROM t").fetchall()
            assert rows == [(2,)]

    def test_pool_configures_wal_and_busy_timeout(self, tmp_path):
        """池化连接也应继承 WAL 和 busy_timeout 策略。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)
        try:
            conn = pool.get_conn()
            conn.execute("CREATE TABLE t (id INTEGER)")
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            pool.close()

        assert journal_mode.lower() == "wal"
        assert busy_timeout >= DEFAULT_BUSY_TIMEOUT_MS

    def test_pool_cleanup_skips_recent(self, tmp_path):
        """清理应在间隔内跳过。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)
        pool._last_cleanup = time.time()  # 设为现在
        pool._cleanup_dead_connections()
        # 不应抛出，且不应执行清理逻辑
        pool.close()

    def test_pool_cleanup_removes_dead_threads(self, tmp_path):
        """清理应移除已终止线程的连接。"""
        db_path = tmp_path / "pool.db"
        pool = SqlitePool(db_path)

        def worker():
            conn = pool.get_conn()
            conn.execute("CREATE TABLE t (id INTEGER)")

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # 线程已终止，但连接仍在字典中
        assert len(pool._conns) == 1

        # 重置清理时间以强制清理
        pool._last_cleanup = 0
        pool._cleanup_dead_connections()

        # 死连接应被移除
        assert len(pool._conns) == 0
        pool.close()


# ---------------------------------------------------------------------------
# validate_sql_identifier
# ---------------------------------------------------------------------------


class TestValidateSqlIdentifier:
    """validate_sql_identifier 白名单校验测试"""

    @pytest.mark.parametrize(
        "name",
        [
            "observations",
            "_id",
            "raw_index",
            "source_type",
            "column_1",
            "A",
            "_",
        ],
    )
    def test_accepts_valid_identifiers(self, name):
        assert validate_sql_identifier(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "1column",
            "column-name",
            "column.name",
            "column;name",
            "column name",
            "column'name",
            'column"name',
            "column\nname",
            "column\tname",
            "*",
            "table; DROP TABLE users;--",
        ],
    )
    def test_rejects_invalid_identifiers(self, name):
        with pytest.raises(ValueError):
            validate_sql_identifier(name)


class TestRenderSql:
    def test_renders_quoted_identifiers_and_generated_placeholders(self):
        query = render_sql(
            "SELECT {columns} FROM {table} WHERE {id_column} IN ({ids})",
            identifiers={"table": "events", "id_column": "event_id"},
            identifier_lists={"columns": ("event_id", "payload_json")},
            placeholder_counts={"ids": 2},
        )

        assert query == (
            'SELECT "event_id", "payload_json" FROM "events" '
            'WHERE "event_id" IN (?, ?)'
        )

    def test_renders_qualified_identifier_paths(self):
        query = render_sql(
            "SELECT name FROM {catalog}",
            identifiers={"catalog": "snapshot.sqlite_master"},
        )

        assert query == 'SELECT name FROM "snapshot"."sqlite_master"'

    def test_rejects_injected_identifier(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            render_sql(
                "SELECT * FROM {table}",
                identifiers={"table": "events; DROP TABLE events"},
            )

    def test_rejects_missing_or_extra_template_tokens(self):
        with pytest.raises(ValueError, match="SQL template tokens"):
            render_sql("SELECT * FROM {table}")
        with pytest.raises(ValueError, match="SQL template tokens"):
            render_sql(
                "SELECT * FROM {table}",
                identifiers={"table": "events", "unused": "other"},
            )

    def test_rejects_empty_placeholder_list(self):
        with pytest.raises(ValueError, match="positive"):
            render_sql(
                "SELECT * FROM events WHERE id IN ({ids})",
                placeholder_counts={"ids": 0},
            )

    def test_fixed_fragments_must_match_their_local_contract(self):
        query = render_sql(
            "SELECT * FROM events WHERE {predicate}",
            fixed_fragments={"predicate": ("trace_id=?", {"trace_id=?", "source=?"})},
        )
        assert query == "SELECT * FROM events WHERE trace_id=?"

        with pytest.raises(ValueError, match="not in its fixed contract"):
            render_sql(
                "SELECT * FROM events WHERE {predicate}",
                fixed_fragments={"predicate": ("1=1; DROP TABLE events", {"trace_id=?"})},
            )
