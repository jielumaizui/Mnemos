"""
Iteration Tracker - 迭代版本追踪器

基于复盘结果自动生成新版本：
1. 读取当前 active 版本
2. 合并新的校验项
3. 写入新版本文件
4. 更新 active 软链接
5. 保留完整版本历史

知识衰减策略：
- 新教训默认 freshness_score=1.0
- 每过一个版本衰减 0.1（最低 0.3）
- 高 hit_count 的教训减缓衰减
"""

import yaml
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .auto_retrospective import RetrospectiveResult
from .pre_flight_injector import PreFlightInjector, ChecklistItem
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


@dataclass
class VersionInfo:
    """版本信息"""
    version: int
    created_at: str
    usage_count: int
    success_rate: float
    path: Path
    is_active: bool = False
    is_deprecated: bool = False


class IterationTracker:
    """迭代版本追踪器"""

    WIKI_BASE = get_config().wiki_dir
    RETROSPECTIVES_DIR = WIKI_BASE / "retrospectives"

    # 知识衰减配置
    FRESHNESS_DECAY_PER_VERSION = 0.1
    FRESHNESS_MIN = 0.3
    HIT_COUNT_PRESERVATION = 0.05  # 每次命中减缓衰减

    # 严格质量门控 — 不符合条件则拒绝生成新版本
    MIN_NEW_LESSONS = 1
    MIN_GAP_SEVERITY = "medium"  # gaps 中至少有一条 >= 此级别
    MIN_SUMMARY_LENGTH = 20
    MIN_CHECKLIST_DELTA_RATIO = 0.2  # 新增 checklist 项占比 >= 20%
    MAX_VERSIONS_PER_DAY = 3  # 同一 task_type/subtype 每天最多生成 3 个版本

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.WIKI_BASE = Path(wiki_base).expanduser()
            self.RETROSPECTIVES_DIR = self.WIKI_BASE / "retrospectives"

    def create_next_version(self, retrospective: RetrospectiveResult) -> Optional[Path]:
        """
        基于复盘结果创建下一个版本

        Args:
            retrospective: 复盘结果

        Returns:
            新版本的文件路径
        """
        task_type = retrospective.task_type
        subtype = retrospective.subtype

        # 1. 获取当前版本
        current = self._get_active_version(task_type, subtype)
        current_version = current.get("version", 0) if current else 0
        current_checklist = self._extract_checklist(current) if current else []
        current_lessons = current.get("lessons_summary", "") if current else ""

        # === 严格质量门控 ===
        # Gate 1: 必须包含新教训或有意义的差距
        has_meaningful_gaps = any(
            g.severity in ("critical", "high", "medium")
            for g in retrospective.gaps
        )
        if len(retrospective.new_lessons) < self.MIN_NEW_LESSONS and not has_meaningful_gaps:
            print(f"[KIA-QualityGate] 拒绝：new_lessons={len(retrospective.new_lessons)} < {self.MIN_NEW_LESSONS} 且 gaps 不足")
            return None

        # Gate 2: 摘要必须非空且有实质内容
        if len(retrospective.summary or "") < self.MIN_SUMMARY_LENGTH:
            print(f"[KIA-QualityGate] 拒绝：summary 长度 {len(retrospective.summary or '')} < {self.MIN_SUMMARY_LENGTH}")
            return None

        # Gate 3: 版本差异必须足够（如果有当前版本）
        if current_checklist:
            new_items_count = len(retrospective.new_lessons)
            # 从 checklist_usage 中也提取未执行的新教训
            for usage in retrospective.checklist_usage:
                if not usage.used and usage.reason_ignored:
                    new_items_count += 1
            delta_ratio = new_items_count / max(len(current_checklist), 1)
            if delta_ratio < self.MIN_CHECKLIST_DELTA_RATIO:
                print(f"[KIA-QualityGate] 拒绝：checklist 增量 {delta_ratio:.0%} < {self.MIN_CHECKLIST_DELTA_RATIO}")
                return None

        # Gate 4: 同一天版本数限制
        if self._count_today_versions(task_type, subtype) >= self.MAX_VERSIONS_PER_DAY:
            print(f"[KIA-QualityGate] 拒绝：今日 {task_type}/{subtype} 已达 {self.MAX_VERSIONS_PER_DAY} 版本上限")
            return None

        # 2. 生成新版本号
        new_version = current_version + 1

        # 3. 衰减现有 checklist
        decayed_checklist = self._apply_freshness_decay(current_checklist)

        # 4. 合并新的校验项
        new_checklist = self._merge_new_lessons(
            decayed_checklist,
            retrospective.new_lessons,
            retrospective.checklist_usage,
            retrospective.gaps
        )

        # 5. 生成新的 lessons_summary
        new_lessons_summary = self._generate_lessons_summary(
            current_lessons,
            retrospective.new_lessons,
            retrospective.gaps
        )

        # 6. 构建 frontmatter
        frontmatter = {
            "hermes_type": "retrospective",
            "task_type": f"{task_type}/{subtype}",
            "version": new_version,
            "previous_version": current_version if current_version > 0 else None,
            "created": datetime.now().isoformat()[:10],
            "status": "active",
            "expected_goals": retrospective.expected_goals,
            "actual_results": retrospective.actual_results,
            "checklist": [self._checklist_item_to_dict(item) for item in new_checklist],
            "lessons_summary": new_lessons_summary,
            "gaps_summary": self._gaps_to_summary(retrospective.gaps),
        }

        # 7. 生成 body
        body = self._generate_body(retrospective)

        # 8. 写入新文件
        new_path = self._write_version(task_type, subtype, new_version, frontmatter, body)

        # 9. 更新 active 链接
        if new_path:
            self._update_active_link(task_type, subtype, new_version)

        # 10. 归档旧版本（如果存在）
        if current:
            self._archive_old_version(task_type, subtype, current_version)

        return new_path

    def _get_active_version(self, task_type: str, subtype: str) -> Optional[Dict]:
        """获取当前 active 版本的 frontmatter"""
        injector = PreFlightInjector(wiki_base=str(self.WIKI_BASE))
        latest = injector._find_latest_version(task_type, subtype)
        if not latest:
            return None

        frontmatter, _ = injector._parse_retrospective(latest)
        return frontmatter

    def _extract_checklist(self, frontmatter: Dict) -> List[ChecklistItem]:
        """从 frontmatter 提取 checklist"""
        raw = frontmatter.get("checklist", [])
        injector = PreFlightInjector(wiki_base=str(self.WIKI_BASE))
        return [injector._parse_checklist_item(item) for item in raw]

    def _apply_freshness_decay(self, checklist: List[ChecklistItem]) -> List[ChecklistItem]:
        """应用知识衰减（六维评估矩阵：活跃度 + 负样本学习）

        衰减公式：
            effective_freshness = base_freshness * (hit_count / (hit_count + ignore_count + 1))
            然后每版本衰减 0.1（最低 0.3）

        负样本（ignore_count）会加速衰减，命中（hit_count）会减缓衰减。
        """
        for item in checklist:
            # 1. 计算热力比率（hit / (hit + ignore + 1)）
            total = item.hit_count + item.ignore_count + 1
            heat_ratio = item.hit_count / total

            # 2. 基础衰减前先应用热力调整
            item.freshness_score = item.freshness_score * heat_ratio

            # 3. 版本基础衰减
            decay = self.FRESHNESS_DECAY_PER_VERSION

            # 4. 命中额外减缓衰减
            preservation = min(item.hit_count * self.HIT_COUNT_PRESERVATION, 0.15)
            decay -= preservation

            # 5. 忽略加速衰减（负样本学习）
            penalty = min(item.ignore_count * 0.03, 0.1)
            decay += penalty

            # 6. 应用衰减
            item.freshness_score = max(
                item.freshness_score - decay,
                self.FRESHNESS_MIN
            )

        return checklist

    def _merge_new_lessons(self, existing: List[ChecklistItem],
                           new_lessons: List[str],
                           checklist_usage: List,
                           gaps: List) -> List[ChecklistItem]:
        """合并新的校验项到现有清单"""
        merged = existing.copy()
        existing_items = {item.item: item for item in merged}

        # 从新增教训生成 checklist 项
        for lesson in new_lessons:
            if lesson in existing_items:
                # 已存在，提升新鲜度
                existing_items[lesson].freshness_score = min(
                    existing_items[lesson].freshness_score + 0.2,
                    1.0
                )
                continue

            # 新项
            severity = self._infer_severity(lesson, gaps)
            trigger_keywords = self._extract_trigger_keywords(lesson)

            new_item = ChecklistItem(
                item=lesson,
                source=f"v{len(merged)} 复盘",
                severity=severity,
                freshness_score=1.0,
                hit_count=0,
                trigger_keywords=trigger_keywords,
            )
            merged.append(new_item)

        # 从未执行的 checklist 项中提取教训
        for usage in checklist_usage:
            if not usage.used and usage.reason_ignored:
                lesson = f"{usage.item}（上次未执行原因：{usage.reason_ignored}）"
                if lesson not in existing_items:
                    merged.append(ChecklistItem(
                        item=lesson,
                        source="复盘-未执行项",
                        severity=usage.severity,
                        freshness_score=1.0,
                        hit_count=0,
                    ))

        return merged

    def _infer_severity(self, lesson: str, gaps) -> str:
        """从 lesson 文本推断严重性"""
        critical_indicators = ["崩", "失败", "损失", "严重", "致命", "全部", "所有"]
        high_indicators = ["不足", "不够", "偏差", "未达", "忽略", "遗漏"]

        for indicator in critical_indicators:
            if indicator in lesson:
                return "critical"
        for indicator in high_indicators:
            if indicator in lesson:
                return "high"

        # 从 gaps 推断
        for gap in gaps:
            if gap.gap in lesson or lesson in gap.gap:
                return gap.severity

        return "medium"

    def _extract_trigger_keywords(self, lesson: str) -> List[str]:
        """从教训文本提取触发关键词"""
        # 简单提取：取 lesson 中的名词性短语
        # 实际实现可以用 jieba 分词，这里用简单规则
        words = lesson.split("，")
        keywords = []
        for w in words:
            w = w.strip()
            if len(w) >= 2:
                keywords.append(w[:10])  # 取前10字
        return keywords[:5]

    def _generate_lessons_summary(self, current: str,
                                   new_lessons: List[str],
                                   gaps) -> str:
        """生成 lessons_summary"""
        lines = []
        if current:
            lines.append(current)
            lines.append("")

        if new_lessons:
            lines.append("新增教训：")
            for lesson in new_lessons:
                lines.append(f"- {lesson}")

        return "\n".join(lines)

    def _gaps_to_summary(self, gaps) -> List[Dict]:
        """转换 gaps 为 summary 格式"""
        return [
            {
                "area": g.area,
                "expected": g.expected,
                "actual": g.actual,
                "gap": g.gap,
                "severity": g.severity,
            }
            for g in gaps
        ]

    def _generate_body(self, retrospective: RetrospectiveResult) -> str:
        """生成文件 body"""
        lines = [
            f"# {retrospective.task_type}/{retrospective.subtype} v{retrospective.version} 复盘",
            "",
            "## 预期 vs 实际",
        ]

        for key, val in retrospective.expected_goals.items():
            actual = retrospective.actual_results.get(key, "未记录")
            lines.append(f"- **{key}**: 预期 {val} → 实际 {actual}")

        if retrospective.gaps:
            lines.extend(["", "## 差异分析"])
            for gap in retrospective.gaps:
                emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(gap.severity, "⚪")
                lines.append(f"{emoji} **{gap.area}**: {gap.gap}")

        if retrospective.new_lessons:
            lines.extend(["", "## 新增教训"])
            for lesson in retrospective.new_lessons:
                lines.append(f"- {lesson}")

        return "\n".join(lines)

    def _checklist_item_to_dict(self, item: ChecklistItem) -> Dict:
        """转换 ChecklistItem 为字典"""
        return {
            "item": item.item,
            "source": item.source,
            "severity": item.severity,
            "freshness_score": round(item.freshness_score, 2),
            "hit_count": item.hit_count,
            "last_hit": item.last_hit,
            "applies_when": item.applies_when,
            "not_applies_when": item.not_applies_when,
            "trigger_keywords": item.trigger_keywords,
            "risk_patterns": item.risk_patterns,
            "detail": item.detail,
        }

    def _write_version(self, task_type: str, subtype: str,
                       version: int, frontmatter: Dict, body: str) -> Optional[Path]:
        """写入版本文件"""
        task_dir = self.RETROSPECTIVES_DIR / task_type
        task_dir.mkdir(parents=True, exist_ok=True)

        file_path = task_dir / f"{subtype}-v{version}.md"

        # 构建完整内容
        content = f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n{body}"

        try:
            file_path.write_text(content, encoding="utf-8")
            return file_path
        except IOError:
            return None

    def _update_active_link(self, task_type: str, subtype: str, version: int):
        """更新 active 软链接"""
        task_dir = self.RETROSPECTIVES_DIR / task_type
        active_link = task_dir / f"{subtype}-active.md"
        target = task_dir / f"{subtype}-v{version}.md"

        try:
            # 删除旧链接
            if active_link.exists() or active_link.is_symlink():
                active_link.unlink()

            # 创建新链接（相对路径）
            active_link.symlink_to(target.name)
        except (OSError, FileExistsError):
            # 如果软链接失败，复制文件
            try:
                import shutil
                shutil.copy2(target, active_link)
            except IOError:
                pass

    def _archive_old_version(self, task_type: str, subtype: str, version: int):
        """归档旧版本"""
        task_dir = self.RETROSPECTIVES_DIR / task_type
        archive_dir = task_dir / ".archive"
        archive_dir.mkdir(exist_ok=True)

        old_file = task_dir / f"{subtype}-v{version}.md"
        if old_file.exists():
            try:
                import shutil
                shutil.move(str(old_file), str(archive_dir / old_file.name))
            except IOError:
                pass

    def _count_today_versions(self, task_type: str, subtype: str) -> int:
        """统计今日已生成版本数"""
        task_dir = self.RETROSPECTIVES_DIR / task_type
        if not task_dir.exists():
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        count = 0
        import re

        for f in task_dir.glob(f"{subtype}-v*.md"):
            try:
                content = f.read_text(encoding="utf-8")
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        import yaml
                        fm = yaml.safe_load(parts[1]) or {}
                        if fm.get("created", "").startswith(today):
                            count += 1
            except Exception:
                continue

        return count

    def list_versions(self, task_type: str, subtype: str) -> List[VersionInfo]:
        """列出所有版本"""
        task_dir = self.RETROSPECTIVES_DIR / task_type
        if not task_dir.exists():
            return []

        versions = []
        import re
        for f in task_dir.glob(f"{subtype}-v*.md"):
            match = re.search(rf'{re.escape(subtype)}-v(\d+)\.md$', f.name)
            if match:
                v = int(match.group(1))
                versions.append(VersionInfo(
                    version=v,
                    created_at="",
                    usage_count=0,
                    success_rate=0.0,
                    path=f,
                    is_active=False
                ))

        # 检查 active 链接
        active_link = task_dir / f"{subtype}-active.md"
        if active_link.exists():
            for v in versions:
                if v.path.resolve() == active_link.resolve():
                    v.is_active = True

        versions.sort(key=lambda x: x.version)
        return versions


    def run_maintenance(self) -> Dict:
        """运行知识状态维护（P/L序列升级/降级）"""
        promoted = 0
        demoted = 0
        scanned = 0

        # 扫描所有 wiki 页面
        inbox_dir = self.WIKI_BASE / "00-Inbox"
        if not inbox_dir.exists():
            return {"promoted": 0, "demoted": 0, "scanned": 0}

        for md_file in inbox_dir.glob("*.md"):
            scanned += 1
            try:
                content = md_file.read_text(encoding="utf-8")
                # 解析 frontmatter
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        import yaml
                        frontmatter = yaml.safe_load(parts[1]) or {}
                        # P序列维护：stage 升级
                        stage = frontmatter.get("stage", "captured")
                        evidence = frontmatter.get("evidence", "single-source")

                        # 简单启发式：如果 evidence 是 multi-source 且 stage 是 captured，提升到 refined
                        if evidence == "multi-source" and stage == "captured":
                            # 这里只是统计，实际升级需要更复杂的逻辑
                            promoted += 1

                        # L序列维护：检查是否过时（基于 temporal 和 created）
                        temporal = frontmatter.get("temporal", "permanent")
                        created = frontmatter.get("created", "")
                        if temporal == "version-bound" and created:
                            try:
                                from datetime import datetime
                                created_date = datetime.strptime(created, "%Y-%m-%d")
                                days_old = (datetime.now() - created_date).days
                                # 超过 90 天的 version-bound 知识标记为可能过时
                                if days_old > 90:
                                    demoted += 1
                            except Exception as e:
                                logger.warning(f"忽略异常: {e}")
            except Exception:
                continue

        return {
            "promoted": promoted,
            "demoted": demoted,
            "scanned": scanned,
        }

    def get_stats(self) -> Dict:
        """获取知识状态统计"""
        total = 0
        p_distribution = {}
        l_distribution = {}

        inbox_dir = self.WIKI_BASE / "00-Inbox"
        if inbox_dir.exists():
            for md_file in inbox_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            import yaml
                            frontmatter = yaml.safe_load(parts[1]) or {}
                            total += 1
                            stage = frontmatter.get("stage", "unknown")
                            level = frontmatter.get("level", "unknown")
                            p_distribution[stage] = p_distribution.get(stage, 0) + 1
                            l_distribution[level] = l_distribution.get(level, 0) + 1
                except Exception:
                    continue

        return {
            "total": total,
            "p_distribution": p_distribution,
            "l_distribution": l_distribution,
        }


# ========== 便捷函数 ==========

def create_next_version(retrospective: RetrospectiveResult) -> Optional[Path]:
    """便捷函数：创建下一版本"""
    tracker = IterationTracker()
    return tracker.create_next_version(retrospective)
