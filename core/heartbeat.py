# -*- coding: utf-8 -*-
"""
Heartbeat - 系统心跳守护

职责：
- 检测配置文件变更 → 热重载
- 检测 Wiki 目录变化 → 触发搜索索引重建
- 定时执行热力衰减
- 定时执行搜索索引全量重建

设计原则：全自动，无需人工审核。
"""

from __future__ import annotations

import json
import re
import sqlite3
import hashlib
import threading
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone

from core.wiki_metrics import WikiMetrics, get_default_metrics
from core.config import get_config


# ==================== _LazyPath ====================

class _LazyPath:
    """Lazy path that resolves get_config() only on access."""
    __slots__ = ('_base', '_segments')

    def __init__(self, base: str = "data_dir", *segments):
        self._base = base
        self._segments = segments

    def __truediv__(self, other):
        return _LazyPath(self._base, *self._segments, other)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def _resolve(self) -> Path:
        config = get_config()
        if self._base == "data_dir":
            result = config.data_dir
        elif self._base == "wiki_dir":
            result = config.wiki_dir
        else:
            result = config.data_dir
        for seg in self._segments:
            result = result / seg
        return result

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return f"LazyPath({self._base}:{'/'.join(self._segments)})"

    def __fspath__(self):
        return str(self._resolve())

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __hash__(self):
        return hash(self._resolve())

    def __eq__(self, other):
        return self._resolve() == other

    def __iter__(self):
        return iter(self._resolve())


DB_PATH = _LazyPath("data_dir", "heartbeat_state.db")
WIKI_DIR = _LazyPath("wiki_dir")


# ==================== HeartbeatDaemon ====================

class HeartbeatDaemon:
    """
    系统心跳守护器

    状态持久化: ~/.mnemos/heartbeat_state.db
    """

    # 监控的配置文件列表（相对 wiki_dir）
    WATCHED_CONFIGS = [
        "config/search.yaml",
        "config/heat_decay.yaml",
    ]

    # Wiki 目录（相对 wiki_dir）
    WIKI_SUBDIRS = [
        "00-Inbox", "01-People", "02-Projects", "03-Tech",
        "04-Concepts", "05-MOCs", "06-Retrospectives",
    ]

    # 默认间隔（秒）
    DEFAULT_INTERVAL = 60

    def __init__(self, interval: int | None = None):
        self.interval = interval or self.DEFAULT_INTERVAL
        self._init_db()

    # ==================== DB 初始化 ====================

    def _init_db(self):
        """初始化状态数据库"""
        db_path = Path(DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_checksums (
                    file_path TEXT PRIMARY KEY,
                    checksum TEXT,
                    last_checked TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wiki_snapshot (
                    dir_name TEXT,
                    file_name TEXT,
                    mtime REAL,
                    size INTEGER,
                    PRIMARY KEY (dir_name, file_name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS heartbeat_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT,
                    detail TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    # ==================== 配置变更检测 ====================

    def check_config_changes(self) -> List[Dict]:
        """
        检测配置文件变更

        Returns:
            变更列表，每项包含 file_path, change_type (new/modified/removed)
        """
        changes = []
        wiki_dir = Path(WIKI_DIR)

        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            cursor = conn.cursor()

            # 读取已保存的 checksum
            cursor.execute("SELECT file_path, checksum FROM config_checksums")
            old_checksums = {row[0]: row[1] for row in cursor.fetchall()}

            current_checksums: Dict[str, str] = {}

            for rel_path in self.WATCHED_CONFIGS:
                file_path = wiki_dir / rel_path

                if not file_path.exists():
                    if rel_path in old_checksums:
                        changes.append({
                            "file_path": rel_path,
                            "change_type": "removed",
                        })
                    continue

                content = file_path.read_text(encoding="utf-8")
                checksum = hashlib.md5(content.encode()).hexdigest()
                current_checksums[rel_path] = checksum

                if rel_path not in old_checksums:
                    changes.append({
                        "file_path": rel_path,
                        "change_type": "new",
                    })
                elif old_checksums[rel_path] != checksum:
                    changes.append({
                        "file_path": rel_path,
                        "change_type": "modified",
                    })

            # 保存新的 checksum
            now = datetime.now(timezone.utc).isoformat()
            for rel_path, checksum in current_checksums.items():
                conn.execute("""
                    INSERT OR REPLACE INTO config_checksums
                    (file_path, checksum, last_checked)
                    VALUES (?, ?, ?)
                """, (rel_path, checksum, now))

            # 清理已删除的记录
            for rel_path in old_checksums:
                if rel_path not in current_checksums:
                    conn.execute(
                        "DELETE FROM config_checksums WHERE file_path = ?",
                        (rel_path,),
                    )

            conn.commit()

        return changes

    # ==================== Wiki 目录变更检测 ====================

    def check_wiki_changes(self) -> Dict:
        """
        检测 Wiki 目录变化

        Returns:
            {"added": [...], "modified": [...], "removed": [...]}
        """
        wiki_path = Path(WIKI_DIR)
        if not wiki_path.exists():
            return {"added": [], "modified": [], "removed": []}

        current_snapshot: Dict[tuple, Dict] = {}
        for subdir in self.WIKI_SUBDIRS:
            dir_path = wiki_path / subdir
            if not dir_path.exists():
                continue
            for file_path in dir_path.glob("*.md"):
                rel_dir = subdir
                rel_name = file_path.name
                stat = file_path.stat()
                current_snapshot[(rel_dir, rel_name)] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }

        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT dir_name, file_name, mtime, size FROM wiki_snapshot"
            )
            old_snapshot = {
                (row[0], row[1]): {"mtime": row[2], "size": row[3]}
                for row in cursor.fetchall()
            }

            added: List[Dict] = []
            modified: List[Dict] = []
            removed: List[Dict] = []

            # 检测新增和修改
            for key, info in current_snapshot.items():
                if key not in old_snapshot:
                    added.append({"dir": key[0], "file": key[1]})
                elif (old_snapshot[key]["mtime"] != info["mtime"] or
                      old_snapshot[key]["size"] != info["size"]):
                    modified.append({"dir": key[0], "file": key[1]})

            # 检测删除
            for key in old_snapshot:
                if key not in current_snapshot:
                    removed.append({"dir": key[0], "file": key[1]})

            # 保存新快照
            cursor.execute("DELETE FROM wiki_snapshot")
            for key, info in current_snapshot.items():
                cursor.execute("""
                    INSERT INTO wiki_snapshot (dir_name, file_name, mtime, size)
                    VALUES (?, ?, ?, ?)
                """, (key[0], key[1], info["mtime"], info["size"]))

            conn.commit()

        return {"added": added, "modified": modified, "removed": removed}

    # ==================== 动作触发 ====================

    def on_config_changed(self, changes: List[Dict]):
        """配置文件变更后的处理"""
        for change in changes:
            path = change["file_path"]
            ctype = change["change_type"]
            detail = f"config {ctype}: {path}"
            print(f"[Heartbeat] {detail}")
            self._log("config_changed", detail)

    def on_wiki_changed(self, changes: Dict):
        """Wiki 目录变更后的处理"""
        total = len(changes["added"]) + len(changes["modified"]) + len(changes["removed"])
        if total == 0:
            return

        detail = (f"added={len(changes['added'])}, "
                  f"modified={len(changes['modified'])}, "
                  f"removed={len(changes['removed'])}")
        print(f"[Heartbeat] Wiki changed: {detail}")
        self._log("wiki_changed", detail)

        # 更新 wiki_metrics（记录文件变更）
        try:
            metrics = get_default_metrics()
            for item in changes["added"] + changes["modified"]:
                page_id = f"{item['dir']}/{item['file']}"
                metrics.upsert_page(
                    path=page_id,
                    freshness_days=0,
                    heat_level="hot",
                )
            print("[Heartbeat] Wiki metrics 更新完成")
        except Exception as e:
            print(f"[Heartbeat] Metrics 更新失败: {e}")

    def run_decay(self) -> Optional[Dict]:
        """执行热力衰减"""
        try:
            metrics = get_default_metrics()
            metrics.decay_all(decay_days=15)
            print("[Heartbeat] 热力衰减完成")
            self._log("decay", "completed")
            return {"status": "ok"}
        except Exception as e:
            print(f"[Heartbeat] 热力衰减失败: {e}")
            self._log("decay_error", str(e))
            return None

    def run_full_index(self) -> Optional[Dict]:
        """执行 wiki_metrics 全量扫描"""
        try:
            metrics = get_default_metrics()
            summary = metrics.get_summary()
            self._log("full_index", f"pages={summary.get('total_pages', 0)}")
            return {"status": "ok", "pages": summary.get("total_pages", 0)}
        except Exception as e:
            print(f"[Heartbeat] Metrics 扫描失败: {e}")
            self._log("full_index_error", str(e))
            return None

    # ==================== 主循环 ====================

    def run_once(self) -> Dict:
        """单次心跳检查"""
        print(f"[Heartbeat] {datetime.now(timezone.utc).isoformat()} 开始检查...")

        # 1. 配置变更检测
        config_changes = self.check_config_changes()
        if config_changes:
            self.on_config_changed(config_changes)

        # 2. Wiki 目录变更检测
        wiki_changes = self.check_wiki_changes()
        if any(wiki_changes.values()):
            self.on_wiki_changed(wiki_changes)

        return {
            "config_changes": config_changes,
            "wiki_changes": wiki_changes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def run_loop(self, stop_event: threading.Event | None = None):
        """
        持续运行心跳循环

        Args:
            stop_event: 外部可设置的停止信号，
                        设置后循环会在当前周期结束后优雅退出。
                        若为 None 则创建内部 Event，仅通过 Ctrl-C 退出。
        """
        if stop_event is None:
            stop_event = threading.Event()

        print(f"[Heartbeat] 启动守护循环，间隔 {self.interval}s")

        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception as e:
                print(f"[Heartbeat] 检查异常: {e}")
                self._log("error", str(e))

            # 用 wait 代替 sleep，可被 stop_event 立即唤醒
            stop_event.wait(timeout=self.interval)

        print("[Heartbeat] 守护循环已退出")

    # ==================== 辅助方法 ====================

    def _log(self, action: str, detail: str):
        """记录心跳日志"""
        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            conn.execute("""
                INSERT INTO heartbeat_log (action, detail)
                VALUES (?, ?)
            """, (action, detail))
            conn.commit()

    def _parse_wiki_content(self, content: str) -> tuple[dict, str]:
        """
        解析 frontmatter 和正文

        使用简单正则解析，不依赖 yaml 库。
        frontmatter 格式示例::

            ---
            title: xxx
            tags: [a, b]
            ---
            正文内容
        """
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm_text = parts[1].strip()
                try:
                    fm = self._parse_simple_frontmatter(fm_text)
                except Exception:
                    fm = {}
                return fm, parts[2].strip()
        return {}, content

    @staticmethod
    def _parse_simple_frontmatter(text: str) -> dict:
        """
        简单正则解析 YAML-like frontmatter

        支持格式:
          key: value
          key: [a, b, c]
        不支持嵌套对象。
        """
        result: dict = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^(\w[\w\s-]*?)\s*:\s*(.+)$', line)
            if not m:
                continue
            key = m.group(1).strip()
            val = m.group(2).strip()
            # 尝试解析 [a, b, c] 格式
            list_match = re.match(r'^\[(.+)\]$', val)
            if list_match:
                items = [item.strip().strip('"').strip("'") for item in list_match.group(1).split(",")]
                result[key] = items
            else:
                result[key] = val
        return result

    def get_stats(self) -> Dict:
        """获取心跳统计"""
        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM heartbeat_log")
            total_logs = cursor.fetchone()[0]

            cursor.execute("""
                SELECT action, COUNT(*) FROM heartbeat_log
                GROUP BY action ORDER BY COUNT(*) DESC
            """)
            by_action = {row[0]: row[1] for row in cursor.fetchall()}

            return {
                "total_logs": total_logs,
                "by_action": by_action,
                "watched_configs": self.WATCHED_CONFIGS,
                "interval": self.interval,
            }


# ==================== CLI ====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Heartbeat Daemon")
    parser.add_argument("--run", action="store_true",
                        help="持续运行守护循环")
    parser.add_argument("--check", action="store_true",
                        help="单次检查")
    parser.add_argument("--decay", action="store_true",
                        help="执行一次热力衰减")
    parser.add_argument("--index", action="store_true",
                        help="执行一次全量索引")
    parser.add_argument("--stats", action="store_true",
                        help="显示统计")
    parser.add_argument("--interval", type=int, default=None,
                        help="守护循环间隔（秒，默认60）")

    args = parser.parse_args()

    daemon = HeartbeatDaemon(interval=args.interval)

    if args.run:
        daemon.run_loop()
    elif args.check:
        result = daemon.run_once()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.decay:
        daemon.run_decay()
    elif args.index:
        daemon.run_full_index()
    elif args.stats:
        stats = daemon.get_stats()
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
