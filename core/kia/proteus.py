# -*- coding: utf-8 -*-
"""
proteus — 知识演化引擎

L3 层：知识版本迭代追踪 + L4 层：知识新鲜度检查
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import logging
import re

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# L4 层：知识新鲜度检查器
# ─────────────────────────────────────────────

@dataclass
class FreshnessAlert:
    """新鲜度告警结果"""
    type: str
    severity: str
    message: str = ""
    action: str = ""


class KnowledgeFreshnessChecker:
    """知识新鲜度检查器 —— L4 轻量模块

    在用户查询结果返回前，检查命中页面的新鲜度，
    生成可选的过时/新版本提醒。
    """

    STALE_DAYS = 90
    VERSION_BOUND_STALE_DAYS = 180

    def __init__(self, half_life_days: int = 30):
        self.half_life_days = half_life_days

    def check(self, page: Dict) -> Optional[FreshnessAlert]:
        """检查页面新鲜度，返回告警或 None"""
        fm = page.get("frontmatter", {}) or {}

        # 1. timeless 页面跳过
        temporal_scope = fm.get("temporal_scope", "") or fm.get("时效性", "")
        if temporal_scope == "timeless" or temporal_scope == "永久":
            return None

        # 2. 版本绑定知识 → 检查是否有新版本
        version_info = fm.get("version_info") or fm.get("版本")
        latest_version = fm.get("latest_version")
        if version_info and latest_version and version_info != latest_version:
            return FreshnessAlert(
                type="newer_version",
                severity="high",
                message=f"⚠️ 基于 {version_info}，最新版本 {latest_version}",
                action="查看更新",
            )

        # 3. 上下文依赖知识 → 检查是否长期未更新
        updated_raw = (
            fm.get("updated_at")
            or fm.get("修改日期")
            or fm.get("last_modified")
            or fm.get("创建日期")
        )
        if updated_raw:
            try:
                age_days = self._parse_age(updated_raw)
                threshold = (
                    self.VERSION_BOUND_STALE_DAYS
                    if temporal_scope == "version-bound"
                    else self.STALE_DAYS
                )
                if age_days > threshold:
                    return FreshnessAlert(
                        type="potentially_stale",
                        severity="medium",
                        message=f"⚠️ 已 {age_days} 天未更新，请确认是否仍有效",
                        action="确认有效性",
                    )
            except (ValueError, TypeError):
                pass

        return None

    def scan_all(self, wiki_base: str) -> List[Dict]:
        """扫描整个 wiki，返回所有过期页面列表"""
        alerts = []
        wiki_path = Path(wiki_base).expanduser()
        if not wiki_path.exists():
            return alerts

        for md_file in wiki_path.rglob("*.md"):
            rel = md_file.relative_to(wiki_path)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if md_file.name.endswith(".shadow.md"):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
                alert = self.check({"frontmatter": fm, "path": str(md_file)})
                if alert:
                    alerts.append({
                        "path": str(md_file),
                        "type": alert.type,
                        "severity": alert.severity,
                        "message": alert.message,
                    })
            except Exception:
                continue
        return alerts

    def _parse_age(self, raw) -> int:
        """解析日期并计算天数"""
        if isinstance(raw, str):
            raw = raw.strip()
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    return (datetime.now() - dt).days
                except ValueError:
                    continue
        elif hasattr(raw, "year"):
            return (datetime.now() - datetime(raw.year, raw.month, raw.day)).days
        return 0

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        """提取 YAML frontmatter"""
        if not content.startswith("---"):
            return {}
        end = content.find("---", 3)
        if end == -1:
            return {}
        try:
            import yaml
            return yaml.safe_load(content[3:end]) or {}
        except Exception:
            return {}


# ─────────────────────────────────────────────
# L3 层：知识版本迭代追踪器
# ─────────────────────────────────────────────

@dataclass
class VersionSnapshot:
    """版本快照"""
    date_str: str
    summary: str
    key_concepts: List[str] = field(default_factory=list)
    complexity_score: float = 0.0


@dataclass
class KnowledgeEvolutionReport:
    """知识演进报告"""
    topic: str
    session_count: int = 0
    versions: List[VersionSnapshot] = field(default_factory=list)
    evolution_path: str = ""

    def to_markdown(self) -> str:
        lines = [
            f"# 知识演化 — {self.topic}",
            f"",
            f"**演进路径**: {self.evolution_path}",
            f"",
            f"**版本数**: {len(self.versions)}",
            f"",
        ]
        for i, v in enumerate(self.versions, 1):
            lines.append(f"### v{i} ({v.date_str})")
            lines.append(f"- {v.summary}")
            if v.key_concepts:
                lines.append(f"- 关键概念: {', '.join(v.key_concepts)}")
            lines.append("")
        return "\n".join(lines)


class IterationTracker:
    """迭代追踪器

    扫描 wiki 页面版本历史，生成知识演化报告，
    管理 retrospectives 目录和 active 软链接。
    """

    MIN_CHECKLIST_DELTA_RATIO = 0.1
    MAX_VERSIONS_PER_DAY = 5

    def __init__(self, wiki_base: str | Path | None = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else None
        self._iterations: List[Dict] = []

    def record_iteration(self, page_path: str, delta: dict = None) -> None:
        """记录一次迭代"""
        self._iterations.append({
            "page_path": page_path,
            "delta": delta or {},
            "timestamp": datetime.now().isoformat(),
        })

    def get_stats(self) -> dict:
        """返回迭代统计"""
        return {
            "total": len(self._iterations),
            "pages": len({i["page_path"] for i in self._iterations}),
        }

    def scan_and_report(self, wiki_base: str | None = None) -> Dict:
        """扫描 wiki 并生成知识演化报告

        Returns:
            {"status": "ok", "reports": int, "topics": List[str]}
        """
        base = Path(wiki_base).expanduser() if wiki_base else self.wiki_base
        if not base or not base.exists():
            return {"status": "error", "error": "wiki_base not found"}

        reports_dir = base / "99-Reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        # 扫描所有页面，按知识类型（task_type / 类型 / 目录）聚类
        clusters: Dict[str, List[Dict]] = {}
        for md_file in base.rglob("*.md"):
            rel = md_file.relative_to(base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if md_file.name.endswith(".shadow.md"):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = KnowledgeFreshnessChecker._extract_frontmatter(content)
                # 按知识类型聚类：task_type > 类型 > 父目录名
                cluster_key = (
                    fm.get("task_type")
                    or fm.get("类型")
                    or fm.get("type")
                    or (rel.parts[0] if rel.parts else "其他")
                )
                if cluster_key not in clusters:
                    clusters[cluster_key] = []
                clusters[cluster_key].append({
                    "path": str(md_file),
                    "frontmatter": fm,
                    "content": content,
                })
            except Exception:
                continue

        generated = 0
        generated_topics = []
        for cluster_key, pages in clusters.items():
            if len(pages) < 2:
                continue
            report = self._build_evolution_report(cluster_key, pages)
            if report and report.versions:
                safe_name = re.sub(r"[^\w\u4e00-\u9fff-]", "-", str(cluster_key))[:30]
                path = reports_dir / f"知识演化-{safe_name}.md"
                path.write_text(report.to_markdown(), encoding="utf-8")
                generated += 1
                generated_topics.append(cluster_key)

        # 创建 active 软链接索引
        self._update_active_links(base, clusters)

        return {
            "status": "ok",
            "reports": generated,
            "topics": generated_topics,
        }

    def _build_evolution_report(self, topic: str, pages: List[Dict]) -> Optional[KnowledgeEvolutionReport]:
        """为单个主题构建演化报告"""
        versions = []
        for page in pages:
            fm = page.get("frontmatter", {})
            updated = (
                fm.get("updated_at")
                or fm.get("修改日期")
                or fm.get("创建日期")
                or ""
            )
            summary = fm.get("摘要") or fm.get("summary") or ""
            concepts = self._extract_concepts(page.get("content", ""))
            versions.append(VersionSnapshot(
                date_str=updated[:10] if updated else "?",
                summary=summary or f"{topic} 笔记",
                key_concepts=concepts,
                complexity_score=self._calculate_complexity(concepts, summary),
            ))

        if len(versions) < 2:
            return None

        versions.sort(key=lambda v: v.date_str)
        first = versions[0]
        latest = versions[-1]

        new_concepts = set(latest.key_concepts) - set(first.key_concepts)
        if latest.complexity_score > first.complexity_score + 0.3:
            evolution = f"从最初的「{first.summary}」，深化到「{latest.summary}」"
        elif new_concepts:
            evolution = (
                f"从「{first.summary}」扩展到「{latest.summary}」，"
                f"新增理解了 {', '.join(list(new_concepts)[:3])}"
            )
        else:
            evolution = f"持续深入「{topic}」，在 {len(versions)} 个版本中不断细化"

        return KnowledgeEvolutionReport(
            topic=topic,
            session_count=len(versions),
            versions=versions,
            evolution_path=evolution,
        )

    def _extract_concepts(self, content: str) -> List[str]:
        """从内容中提取关键概念（[[wikilink]] 和代码术语）"""
        concepts: Set[str] = set()
        # 提取 [[wikilink]]
        for match in re.finditer(r"\[\[([^\]]+)\]\]", content):
            concepts.add(match.group(1).strip())
        # 提取 `inline_code` 和代码块中的术语
        for match in re.finditer(r"`([^`]+)`", content):
            term = match.group(1).strip()
            if len(term) > 2 and not term.startswith("http"):
                concepts.add(term)
        return sorted(concepts)[:20]

    def _calculate_complexity(self, concepts: List[str], summary: str) -> float:
        """计算复杂度评分"""
        concept_score = min(len(concepts) / 10, 1.0)
        length_score = min(len(summary) / 100, 1.0)
        return round((concept_score + length_score) / 2, 2)

    def _update_active_links(self, base: Path, topics: Dict[str, List[Dict]]) -> None:
        """更新 active 软链接索引文件"""
        retros_dir = base / "06-Retrospectives"
        retros_dir.mkdir(parents=True, exist_ok=True)

        active_index = retros_dir / "_active-index.md"
        lines = ["# Active 知识索引", "", "> 当前最新版本的知识页面", ""]

        for topic, pages in sorted(topics.items()):
            if len(pages) < 1:
                continue
            # 取最近更新的页面作为 active
            latest = max(
                pages,
                key=lambda p: p.get("frontmatter", {}).get("修改日期", "")
                or p.get("frontmatter", {}).get("创建日期", "")
                or "",
            )
            rel_path = Path(latest["path"]).relative_to(base)
            lines.append(f"- [[{rel_path.with_suffix('').as_posix()}|{topic}]]")

        active_index.write_text("\n".join(lines), encoding="utf-8")
