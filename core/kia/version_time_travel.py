"""
Version Time Travel - 知识版本时间旅行

记录知识页面的修改历史，支持：
1. 创建快照（保存当前版本）
2. 查看 diff（任意两个版本的对比）
3. 版本列表（时间线视图）
4. 回溯恢复（恢复到历史版本）

设计原则：
- 基于文件快照，不依赖 Git
- 增量存储（相同内容不重复保存）
- diff 输出 Markdown 格式，便于阅读
- 与 frontmatter 的 "演化历史" 章节联动
"""

import hashlib
import json
import difflib
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


@dataclass
class VersionSnapshot:
    """版本快照"""
    snapshot_id: str           # hash of content
    timestamp: str
    content_hash: str
    change_summary: str = ""
    size_bytes: int = 0


@dataclass
class VersionDiff:
    """版本差异"""
    from_version: str
    to_version: str
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)
    modified_sections: List[Dict] = field(default_factory=list)
    frontmatter_changes: Dict = field(default_factory=dict)


class VersionTimeTravel:
    """版本时间旅行器"""

    def __init__(self, wiki_base: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.snapshot_dir = self.wiki_base / ".kg" / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.snapshot_dir / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> Dict:
        """加载快照索引"""
        if self.index_file.exists():
            try:
                return json.loads(self.index_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save_index(self):
        """保存快照索引"""
        try:
            self.index_file.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except IOError:
            pass

    def snapshot(self, page_path: Path,
                 change_summary: str = "") -> Optional[VersionSnapshot]:
        """
        为页面创建快照

        Returns:
            快照信息，如果内容未变化则返回 None
        """
        if not page_path.exists():
            return None

        try:
            content = page_path.read_text(encoding="utf-8")
        except Exception:
            return None

        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        page_key = str(page_path.relative_to(self.wiki_base))

        # 检查是否与上一个快照相同
        page_history = self._index.get(page_key, [])
        if page_history and page_history[-1]["content_hash"] == content_hash:
            return None  # 内容未变化

        # 保存快照文件
        snapshot_id = f"{content_hash[:16]}"
        snapshot_path = self.snapshot_dir / f"{snapshot_id}.md"

        if not snapshot_path.exists():
            snapshot_path.write_text(content, encoding="utf-8")

        snapshot = VersionSnapshot(
            snapshot_id=snapshot_id,
            timestamp=datetime.now().isoformat()[:19],
            content_hash=content_hash,
            change_summary=change_summary,
            size_bytes=len(content.encode("utf-8")),
        )

        # 更新索引
        if page_key not in self._index:
            self._index[page_key] = []

        self._index[page_key].append({
            "snapshot_id": snapshot_id,
            "timestamp": snapshot.timestamp,
            "content_hash": content_hash,
            "change_summary": change_summary,
            "size_bytes": snapshot.size_bytes,
        })

        self._save_index()
        return snapshot

    def list_versions(self, page_path: Path) -> List[VersionSnapshot]:
        """列出页面的所有版本"""
        page_key = str(page_path.relative_to(self.wiki_base))
        history = self._index.get(page_key, [])

        return [
            VersionSnapshot(
                snapshot_id=h["snapshot_id"],
                timestamp=h["timestamp"],
                content_hash=h["content_hash"],
                change_summary=h.get("change_summary", ""),
                size_bytes=h.get("size_bytes", 0),
            )
            for h in history
        ]

    def get_version_content(self, snapshot_id: str) -> Optional[str]:
        """获取指定版本的内容"""
        snapshot_path = self.snapshot_dir / f"{snapshot_id}.md"
        if snapshot_path.exists():
            try:
                return snapshot_path.read_text(encoding="utf-8")
            except IOError:
                pass
        return None

    def diff(self, page_path: Path,
             from_snapshot: str = None,
             to_snapshot: str = None) -> Optional[VersionDiff]:
        """
        比较两个版本

        Args:
            page_path: 页面路径
            from_snapshot: 起始版本 ID（None 表示当前版本）
            to_snapshot: 目标版本 ID（None 表示上一个版本）

        Returns:
            VersionDiff
        """
        versions = self.list_versions(page_path)
        if len(versions) < 2:
            return None

        # 默认：比较最后两个版本
        if to_snapshot is None and len(versions) >= 2:
            to_snapshot = versions[-1].snapshot_id
            from_snapshot = versions[-2].snapshot_id
        elif from_snapshot is None:
            from_snapshot = versions[0].snapshot_id

        # 获取内容
        from_content = self.get_version_content(from_snapshot)
        to_content = self.get_version_content(to_snapshot)

        if from_content is None or to_content is None:
            return None

        # 生成 unified diff
        from_lines = from_content.splitlines(keepends=True)
        to_lines = to_content.splitlines(keepends=True)

        diff_lines = list(difflib.unified_diff(
            from_lines, to_lines,
            fromfile=f"v-{from_snapshot[:8]}",
            tofile=f"v-{to_snapshot[:8]}",
            lineterm="",
        ))

        # 解析 diff
        added = []
        removed = []
        for line in diff_lines:
            if line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:].rstrip())
            elif line.startswith("-") and not line.startswith("---"):
                removed.append(line[1:].rstrip())

        # 检测 frontmatter 变化
        fm_changes = self._detect_frontmatter_changes(from_content, to_content)

        # 检测章节变化
        section_changes = self._detect_section_changes(from_content, to_content)

        return VersionDiff(
            from_version=from_snapshot,
            to_version=to_snapshot,
            added_lines=added,
            removed_lines=removed,
            modified_sections=section_changes,
            frontmatter_changes=fm_changes,
        )

    def restore(self, page_path: Path,
                snapshot_id: str,
                create_backup: bool = True) -> bool:
        """
        恢复到指定版本

        Args:
            page_path: 页面路径
            snapshot_id: 目标版本 ID
            create_backup: 是否先备份当前版本

        Returns:
            是否成功
        """
        content = self.get_version_content(snapshot_id)
        if content is None:
            return False

        # 先备份当前版本
        if create_backup:
            self.snapshot(page_path, change_summary="自动备份（恢复前）")

        try:
            page_path.write_text(content, encoding="utf-8")
            # 恢复后再创建一个新快照
            self.snapshot(page_path, change_summary=f"恢复到版本 {snapshot_id[:8]}")
            return True
        except IOError:
            return False

    def generate_timeline(self, page_path: Path) -> str:
        """生成版本时间线 Markdown"""
        versions = self.list_versions(page_path)
        if not versions:
            return "该页面暂无版本历史。\n"

        lines = [
            f"# 版本时间线: {page_path.stem}",
            f"共 {len(versions)} 个版本",
            "",
        ]

        for i, v in enumerate(versions):
            marker = "🔄" if i == len(versions) - 1 else ""
            lines.append(f"### {marker} {v.timestamp} — `{v.snapshot_id[:8]}`")
            if v.change_summary:
                lines.append(f"_{v.change_summary}_")
            lines.append(f"大小: {v.size_bytes} bytes")
            lines.append("")

        return "\n".join(lines)

    def diff_to_markdown(self, diff: VersionDiff) -> str:
        """将 diff 转换为 Markdown 格式"""
        lines = [
            f"# 版本对比",
            f"**{diff.from_version[:8]}** → **{diff.to_version[:8]}**",
            "",
        ]

        if diff.frontmatter_changes:
            lines.extend(["## Frontmatter 变更", ""])
            for key, change in diff.frontmatter_changes.items():
                lines.append(f"- **{key}**: `{change.get('old', '')}` → `{change.get('new', '')}`")
            lines.append("")

        if diff.modified_sections:
            lines.extend(["## 章节变更", ""])
            for section in diff.modified_sections:
                action = section.get("action", "修改")
                lines.append(f"- [{action}] {section.get('name', '未知章节')}")
            lines.append("")

        if diff.added_lines:
            lines.extend(["## 新增内容", "```diff", "+ " + "\n+ ".join(diff.added_lines[:20]), "```", ""])

        if diff.removed_lines:
            lines.extend(["## 删除内容", "```diff", "- " + "\n- ".join(diff.removed_lines[:20]), "```", ""])

        return "\n".join(lines)

    def scan_and_snapshot_all(self) -> Dict[str, int]:
        """扫描所有页面，为变更的页面创建快照"""
        stats = {"scanned": 0, "snapshotted": 0, "unchanged": 0}

        if not self.inbox.exists():
            return stats

        for page in self.inbox.glob("*.md"):
            stats["scanned"] += 1
            result = self.snapshot(page)
            if result:
                stats["snapshotted"] += 1
            else:
                stats["unchanged"] += 1

        return stats

    # ========== 辅助方法 ==========

    @staticmethod
    def _detect_frontmatter_changes(old_content: str, new_content: str) -> Dict:
        """检测 frontmatter 变化"""
        import yaml

        def extract_fm(content):
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        return yaml.safe_load(parts[1]) or {}
                    except Exception as e:
                        logger.warning(f"忽略异常: {e}")
            return {}

        old_fm = extract_fm(old_content)
        new_fm = extract_fm(new_content)

        changes = {}
        all_keys = set(old_fm.keys()) | set(new_fm.keys())
        for key in all_keys:
            old_val = old_fm.get(key)
            new_val = new_fm.get(key)
            if old_val != new_val:
                changes[key] = {"old": old_val, "new": new_val}

        return changes

    @staticmethod
    def _detect_section_changes(old_content: str, new_content: str) -> List[Dict]:
        """检测章节变化"""
        import re

        def extract_sections(content):
            body = content
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    body = parts[2]

            sections = {}
            current_name = "_开头"
            current_lines = []

            for line in body.split("\n"):
                match = re.match(r"^(#{2,4})\s+(.+)$", line)
                if match:
                    sections[current_name] = current_lines
                    current_name = match.group(2).strip()
                    current_lines = [line]
                else:
                    current_lines.append(line)

            sections[current_name] = current_lines
            return sections

        old_sections = extract_sections(old_content)
        new_sections = extract_sections(new_content)

        changes = []
        for name in set(new_sections.keys()) - set(old_sections.keys()):
            changes.append({"name": name, "action": "新增"})

        for name in set(old_sections.keys()) - set(new_sections.keys()):
            changes.append({"name": name, "action": "删除"})

        for name in set(old_sections.keys()) & set(new_sections.keys()):
            if old_sections[name] != new_sections[name]:
                changes.append({"name": name, "action": "修改"})

        return changes


# ========== 便捷函数 ==========

def snapshot_page(page_path: str, change_summary: str = "") -> Optional[VersionSnapshot]:
    """便捷函数：为页面创建快照"""
    traveler = VersionTimeTravel()
    return traveler.snapshot(Path(page_path), change_summary)


def show_diff(page_path: str) -> Optional[str]:
    """便捷函数：显示页面最近的 diff"""
    traveler = VersionTimeTravel()
    diff = traveler.diff(Path(page_path))
    if diff:
        return traveler.diff_to_markdown(diff)
    return "暂无版本历史或内容未变化。"
