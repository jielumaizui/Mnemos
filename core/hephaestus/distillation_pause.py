# -*- coding: utf-8 -*-
"""Pause-state persistence for the distillation pipeline."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.config import get_config
from core.runtime_environment import environment_get

logger = logging.getLogger(__name__)

RESUME_AFTER_SECONDS = 300  # 5 分钟后自动尝试恢复


def _get_pause_db() -> Path:
    """Resolve pause state inside a hermetic run even if config was cached earlier."""
    configured = Path(get_config().database_dir).expanduser() / "distillation_state.db"
    run_root = environment_get("MNEMOS_RUN_ROOT")
    run_database = environment_get("MNEMOS_DATABASE_DIR")
    if not run_root or not run_database:
        return configured
    root = Path(run_root).expanduser().resolve(strict=False)
    database_root = Path(run_database).expanduser().resolve(strict=False)
    if database_root != root and root not in database_root.parents:
        raise ValueError(
            "hermetic MNEMOS_DATABASE_DIR escapes MNEMOS_RUN_ROOT; refusing pause-state write"
        )
    configured_parent = configured.parent.resolve(strict=False)
    if configured_parent == root or root in configured_parent.parents:
        return configured
    return database_root / "distillation_state.db"


def _init_pause_table():
    db = _get_pause_db()
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db), timeout=5) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS distillation_pause_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                paused INTEGER DEFAULT 0,
                reason TEXT,
                paused_at TEXT,
                resume_at TEXT,
                api_chain_desc TEXT,
                last_error TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO distillation_pause_state (id, paused)
            VALUES (1, 0)
        """)


def is_distillation_paused() -> bool:
    """检查蒸馏是否处于暂停状态（含自动恢复逻辑）。"""
    _init_pause_table()
    db = _get_pause_db()
    with sqlite3.connect(str(db), timeout=5) as conn:
        row = conn.execute(
            "SELECT paused, resume_at FROM distillation_pause_state WHERE id = 1"
        ).fetchone()
        if not row or not row[0]:
            return False
        resume_at = row[1]
        if resume_at:
            try:
                resume_dt = datetime.fromisoformat(resume_at)
                if datetime.now(timezone.utc) >= resume_dt:
                    # 自动恢复
                    conn.execute(
                        "UPDATE distillation_pause_state SET paused = 0, reason = NULL, resume_at = NULL WHERE id = 1"  # noqa: E501
                    )
                    logger.info("[Distillation] 自动恢复：倒计时结束，蒸馏继续")
                    return False
            except ValueError:
                logger.warning("[Distillation] 恢复时间解析失败: %s", resume_at, exc_info=True)
        return True


def pause_distillation(
    reason: str = "",
    resume_after: int = RESUME_AFTER_SECONDS,
    api_chain_desc: str = "",
    last_error: str = "",
):
    """暂停蒸馏，设置自动恢复倒计时。"""
    _init_pause_table()
    db = _get_pause_db()
    resume_at = (datetime.now(timezone.utc) + timedelta(seconds=resume_after)).isoformat()
    with sqlite3.connect(str(db), timeout=5) as conn:
        conn.execute(
            """UPDATE distillation_pause_state
               SET paused = 1, reason = ?, paused_at = ?, resume_at = ?,
                   api_chain_desc = ?, last_error = ?
               WHERE id = 1""",
            (reason, datetime.now(timezone.utc).isoformat(), resume_at, api_chain_desc, last_error),
        )
    logger.info("[Distillation] 蒸馏已暂停，原因: %s，将在 %s 秒后自动恢复", reason, resume_after)


def resume_distillation():
    """手动恢复蒸馏。"""
    _init_pause_table()
    db = _get_pause_db()
    with sqlite3.connect(str(db), timeout=5) as conn:
        conn.execute(
            "UPDATE distillation_pause_state SET paused = 0, reason = NULL, resume_at = NULL WHERE id = 1"  # noqa: E501
        )
    logger.info("[Distillation] 蒸馏已手动恢复")


def get_pause_status() -> dict:
    """只读获取当前暂停状态（用于 CLI/状态查询）。

    状态查询不会创建数据库、目录或表；初始化只属于 pause/resume 写路径。
    """
    db = _get_pause_db()
    if not db.is_file():
        return {"paused": False}
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='distillation_pause_state'"
        ).fetchone()
        if not table_exists:
            return {"paused": False}
        row = conn.execute(
            "SELECT paused, reason, paused_at, resume_at, api_chain_desc, last_error "
            "FROM distillation_pause_state WHERE id = 1"
        ).fetchone()
    if not row:
        return {"paused": False}
    return {
        "paused": bool(row[0]),
        "reason": row[1],
        "paused_at": row[2],
        "resume_at": row[3],
        "api_chain_desc": row[4],
        "last_error": row[5],
    }
