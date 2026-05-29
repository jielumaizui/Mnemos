# -*- coding: utf-8 -*-
"""
SQLite 连接安全工具

问题：Python 内置 sqlite3.connect 的上下文管理器只处理事务（commit/rollback），
      不会自动调用 close()。在高频循环中（如 daemon 的 CaptureQueue 每几秒 tick
      一次），重复打开不关闭的连接会导致文件描述符泄漏，最终触发
      "OSError: [Errno 24] Too many open files"。

本模块提供 drop-in 替代：
  - sqlite_conn()  — 行为与 sqlite3.connect() 完全一致，但 with 退出时自动 close()
  - SqlitePool    — 为高频组件（CaptureQueue / SyncEngine）提供持久连接复用

用法：
    # 旧代码（泄漏）
    with sqlite3.connect(path, timeout=10) as conn:
        ...

    # 新代码（安全）
    from core.db_utils import sqlite_conn
    with sqlite_conn(path, timeout=10) as conn:
        ...

    # 高频组件（持久连接）
    pool = SqlitePool(path)
    conn = pool.get_conn()
    ...
    pool.close()
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


@contextmanager
def sqlite_conn(*args, **kwargs):
    """sqlite3.connect 的安全替代：with 块退出时自动 close()。

    事务行为与原始 sqlite3.connect 上下文管理器完全一致：
    - 无异常时 commit
    - 有异常时 rollback
    - 最终无论是否异常都 close
    """
    conn = sqlite3.connect(*args, **kwargs)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


class SqlitePool:
    """SQLite 持久连接池（按线程隔离连接）。

    适用于高频访问场景（如 CaptureQueue、SyncEngine），避免每操作一次
    都新建/销毁连接。每个线程拥有独立连接，避免 SQLite 线程限制。
    """

    def __init__(self, db_path: Path, timeout: int = 10):
        self.db_path = Path(db_path)
        self._timeout = timeout
        self._conns: Dict[int, sqlite3.Connection] = {}
        self._lock = threading.Lock()

    def get_conn(self) -> sqlite3.Connection:
        """获取（或创建）当前线程的持久连接。"""
        tid = threading.current_thread().ident
        if tid not in self._conns:
            with self._lock:
                if tid not in self._conns:
                    self._conns[tid] = sqlite3.connect(
                        str(self.db_path), timeout=self._timeout, check_same_thread=False
                    )
        conn = self._conns[tid]
        conn.row_factory = None
        # 防御：如果上次异常退出留下挂起事务，自动回滚
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:
            pass
        return conn

    def close(self) -> None:
        """关闭所有线程的持久连接。"""
        with self._lock:
            for conn in list(self._conns.values()):
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()

    def __enter__(self) -> "SqlitePool":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
