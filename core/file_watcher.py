"""
File Watcher - 文件监控与增量处理

职责：
- 监控 Wiki 目录的文件变化（新增、修改、删除）
- 触发增量 ingest（只处理变更文件）
- 支持批量防抖（避免频繁触发）

设计原则：
- 使用轮询（跨平台兼容，不依赖 watchdog/fsevents）
- 记录文件 mtime + size 作为变更检测依据
- 批量处理，防抖间隔 5 秒
"""

import sqlite3
import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass
from datetime import datetime

WIKI_DB = Path.home() / ".claude" / "wiki_state.db"


@dataclass
class FileChange:
    """文件变更记录"""
    path: str
    change_type: str       # added / modified / deleted
    old_mtime: Optional[float]
    new_mtime: Optional[float]
    old_size: Optional[int]
    new_size: Optional[int]


class FileWatcher:
    """文件监控器"""

    DEBOUNCE_SECONDS = 5

    def __init__(self, watch_dir: Optional[Path] = None, db_path: Optional[Path] = None):
        self.watch_dir = watch_dir or (Path.home() / "Documents" / "Obsidian Vault" / "wiki")
        self.db_path = db_path or WIKI_DB
        self._ensure_table()
        self._last_scan_time = 0

    def _ensure_table(self):
        """确保文件状态表存在"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_snapshots (
                    path TEXT PRIMARY KEY,
                    mtime REAL,
                    size INTEGER,
                    fingerprint TEXT,
                    scanned_at TEXT
                )
            """)
            conn.commit()

    def _file_fingerprint(self, path: Path) -> str:
        """计算文件指纹（基于内容 hash）"""
        try:
            content = path.read_bytes()
            return hashlib.md5(content).hexdigest()[:16]
        except Exception:
            return ""

    def _get_stored_state(self) -> Dict[str, Dict]:
        """获取已存储的文件状态"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute("SELECT path, mtime, size, fingerprint FROM file_snapshots")
            return {
                row[0]: {
                    "mtime": row[1],
                    "size": row[2],
                    "fingerprint": row[3],
                }
                for row in cursor.fetchall()
            }

    def _update_state(self, path: str, mtime: float, size: int, fingerprint: str):
        """更新文件状态"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO file_snapshots (path, mtime, size, fingerprint, scanned_at)
                VALUES (?, ?, ?, ?, ?)
            """, (path, mtime, size, fingerprint, datetime.now().isoformat()))
            conn.commit()

    def _remove_state(self, path: str):
        """删除文件状态"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("DELETE FROM file_snapshots WHERE path = ?", (path,))
            conn.commit()

    def scan(self) -> List[FileChange]:
        """
        扫描目录变化

        Returns:
            变更列表
        """
        now = time.time()
        if now - self._last_scan_time < self.DEBOUNCE_SECONDS:
            return []  # 防抖

        self._last_scan_time = now

        stored = self._get_stored_state()
        changes = []
        current_paths = set()

        # 扫描当前文件
        if self.watch_dir.exists():
            for file_path in self.watch_dir.rglob("*.md"):
                path_str = str(file_path)
                current_paths.add(path_str)

                try:
                    stat = file_path.stat()
                    mtime = stat.st_mtime
                    size = stat.st_size
                    fingerprint = self._file_fingerprint(file_path)
                except Exception:
                    continue

                if path_str not in stored:
                    # 新增文件
                    changes.append(FileChange(
                        path=path_str,
                        change_type="added",
                        old_mtime=None,
                        new_mtime=mtime,
                        old_size=None,
                        new_size=size,
                    ))
                    self._update_state(path_str, mtime, size, fingerprint)
                else:
                    old = stored[path_str]
                    # 检查是否变更（mtime 或 size 或 fingerprint）
                    if (old["mtime"] != mtime or
                        old["size"] != size or
                        old["fingerprint"] != fingerprint):
                        changes.append(FileChange(
                            path=path_str,
                            change_type="modified",
                            old_mtime=old["mtime"],
                            new_mtime=mtime,
                            old_size=old["size"],
                            new_size=size,
                        ))
                        self._update_state(path_str, mtime, size, fingerprint)

        # 检测删除的文件
        for path_str in stored:
            if path_str not in current_paths:
                changes.append(FileChange(
                    path=path_str,
                    change_type="deleted",
                    old_mtime=stored[path_str]["mtime"],
                    new_mtime=None,
                    old_size=stored[path_str]["size"],
                    new_size=None,
                ))
                self._remove_state(path_str)

        return changes

    def watch(self, callback: Callable[[List[FileChange]], None],
              interval: float = 30.0):
        """
        持续监控（阻塞）

        Args:
            callback: 变更回调函数
            interval: 扫描间隔（秒）
        """
        while True:
            changes = self.scan()
            if changes:
                callback(changes)
            time.sleep(interval)

    def get_changed_files(self, since: Optional[datetime] = None) -> List[str]:
        """
        获取自某时间点以来变更的文件

        Args:
            since: 起始时间

        Returns:
            文件路径列表
        """
        if since is None:
            # 返回所有已知的文件
            stored = self._get_stored_state()
            return list(stored.keys())

        since_str = since.isoformat()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT path FROM file_snapshots WHERE scanned_at > ?",
                (since_str,)
            )
            return [row[0] for row in cursor.fetchall()]


# ========== 便捷函数 ==========

def scan_changes() -> List[FileChange]:
    """便捷函数：扫描一次变更"""
    watcher = FileWatcher()
    return watcher.scan()


def quick_check(watch_dir: str) -> List[str]:
    """便捷函数：快速检查变更文件路径"""
    watcher = FileWatcher(watch_dir=Path(watch_dir))
    changes = watcher.scan()
    return [c.path for c in changes]
