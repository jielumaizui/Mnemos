# -*- coding: utf-8 -*-
"""数据库维护相关 CLI 命令。"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from argparse import Namespace
from daemon.maintenance import DatabaseMaintenanceTask

logger = logging.getLogger(__name__)


def cmd_db_maintenance(args: Namespace) -> int:
    """执行数据库存留清理与维护（支持 --dry-run 预览）。"""
    dry_run = bool(getattr(args, "dry_run", False))
    task = DatabaseMaintenanceTask()
    try:
        result = task.run(dry_run=dry_run, force=True)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        logger.error("[DBMaintenance] 运行失败: %s", exc, exc_info=True)
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
