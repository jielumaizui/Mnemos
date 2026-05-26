# -*- coding: utf-8 -*-
"""
EvolutionTracker — 知识演化跟踪

TemporalEvolutionTracker  — 版本绑定 + 上下文时间范围检测
DarkKnowledgeIntegration — erebus 差距 / 演变 / 关联驱动策略调整
RecirculationGuard       — 防止 Wiki 引用内容再次蒸馏
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import get_config

logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"


# ========== 数据模型 ==========

@dataclass
class EvolutionAlert:
    """演化告警"""
    entity: str
    alert_type: str  # version_outdated / context_expired / rarely_accessed / contradicted
    detail: str = ""
    wiki_page: str = ""
    severity: float = 0.5  # 0-1
    created_at: str = ""


@dataclass
class TemporalScope:
    """时效范围"""
    scope_type: str  # permanent / stable / version-bound / contextual
    version: str = ""
    context_date: str = ""
    expires_after_days: int = 0  # 0 = never

    @property
    def is_expired(self) -> bool:
        if self.scope_type == "permanent":
            return False
        if self.scope_type == "stable":
            return False
        if not self.context_date:
            return False
        try:
            created = datetime.fromisoformat(self.context_date)
            age_days = (datetime.now() - created).days
            if self.scope_type == "version-bound":
                return age_days > 365  # 版本绑定知识 1 年后标记过期
            if self.scope_type == "contextual":
                return age_days > self.expires_after_days if self.expires_after_days > 0 else age_days > 90
        except Exception:
            pass
        return False


# ========== TemporalEvolutionTracker ==========

class TemporalEvolutionTracker:
    """时间演化跟踪器

    检测知识的时效性变化：
    1. 版本绑定检查（v1.x 相关知识在新版本下可能失效）
    2. 上下文过期检测（90 天无访问的上下文知识标记过期）
    3. 30 天无访问衰减
    """

    ALERT_TABLE = """
        CREATE TABLE IF NOT EXISTS evolution_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            wiki_page TEXT DEFAULT '',
            severity REAL DEFAULT 0.5,
            created_at TEXT,
            resolved INTEGER DEFAULT 0
        )
    """

    def __init__(self):
        self._db_path = _get_db_path()
        self._init_db()

    def _init_db(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path), timeout=5) as conn:
            conn.execute(self.ALERT_TABLE)
            conn.commit()

    def check_entity_freshness(self, entity: str, wiki_page: Path,
                                recent_sessions: List[Dict] = None) -> Optional[EvolutionAlert]:
        """检查实体知识新鲜度

        Args:
            entity: 实体名称
            wiki_page: Wiki 页面路径
            recent_sessions: 近期会话列表（用于检测版本更新）

        Returns:
            EvolutionAlert 如果知识可能过时，否则 None
        """
        try:
            content = wiki_page.read_text(encoding="utf-8")
        except Exception:
            return None

        scope = self._extract_temporal_scope(content)
        if not scope:
            return None

        # 版本绑定检查
        if scope.scope_type == "version-bound" and scope.is_expired:
            return EvolutionAlert(
                entity=entity,
                alert_type="version_outdated",
                detail=f"版本绑定知识已超过 1 年（版本: {scope.version}）",
                wiki_page=str(wiki_page),
                severity=0.7,
                created_at=datetime.now().isoformat(),
            )

        # 上下文过期检查
        if scope.scope_type == "contextual" and scope.is_expired:
            return EvolutionAlert(
                entity=entity,
                alert_type="context_expired",
                detail=f"上下文知识可能已过时（创建于 {scope.context_date}）",
                wiki_page=str(wiki_page),
                severity=0.5,
                created_at=datetime.now().isoformat(),
            )

        # 最近会话中是否有版本升级信号
        if recent_sessions and scope.version:
            for session in recent_sessions[-5:]:
                session_text = session.get("content", "")
                upgrade_pattern = rf'{re.escape(scope.version.split(".")[0])}\.\d+'
                if re.search(r'(升级|迁移|更新|upgrade|migrate|update)', session_text, re.I):
                    newer_versions = re.findall(upgrade_pattern, session_text)
                    if newer_versions and newer_versions[0] != scope.version:
                        return EvolutionAlert(
                            entity=entity,
                            alert_type="version_outdated",
                            detail=f"检测到新版本 {newer_versions[0]}（当前: {scope.version}）",
                            wiki_page=str(wiki_page),
                            severity=0.8,
                            created_at=datetime.now().isoformat(),
                        )

        # 30 天无访问衰减
        access_time = self._get_last_access(wiki_page)
        if access_time:
            age_days = (datetime.now() - access_time).days
            if age_days > 30:
                return EvolutionAlert(
                    entity=entity,
                    alert_type="rarely_accessed",
                    detail=f"知识页面 {age_days} 天未被访问",
                    wiki_page=str(wiki_page),
                    severity=min(0.6, age_days / 180),
                    created_at=datetime.now().isoformat(),
                )

        return None

    def scan_all_pages(self, wiki_dir: Path) -> List[EvolutionAlert]:
        """扫描所有 Wiki 页面，检测过时知识"""
        alerts = []
        for subdir in ["00-Inbox", "01-Projects", "02-Areas", "03-Tech", "04-Concepts"]:
            md_dir = wiki_dir / subdir
            if not md_dir.exists():
                continue
            for md_file in md_dir.glob("*.md"):
                entity = md_file.stem
                alert = self.check_entity_freshness(entity, md_file)
                if alert:
                    alerts.append(alert)
                    self._save_alert(alert)
        return alerts

    def _extract_temporal_scope(self, content: str) -> Optional[TemporalScope]:
        """从页面内容提取时效范围"""
        # 解析 frontmatter
        fm = self._parse_frontmatter(content)
        if not fm:
            return None

        temporal = fm.get("时效性", fm.get("temporal", ""))
        version = fm.get("版本标记", fm.get("version", ""))
        created = fm.get("创建日期", fm.get("created", ""))

        if not temporal:
            return None

        scope_type = temporal.lower().replace("-", "_")
        if scope_type not in ("permanent", "stable", "version_bound", "contextual"):
            return None

        return TemporalScope(
            scope_type=scope_type,
            version=version,
            context_date=created,
        )

    @staticmethod
    def _parse_frontmatter(content: str) -> Optional[Dict]:
        if not content.startswith("---"):
            return None
        end = content.find("---", 3)
        if end == -1:
            return None
        fm = {}
        for line in content[3:end].strip().split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip()
        return fm

    @staticmethod
    def _get_last_access(page: Path) -> Optional[datetime]:
        """获取最后访问时间（使用 mtime 近似）"""
        try:
            mtime = page.stat().st_mtime
            return datetime.fromtimestamp(mtime)
        except Exception:
            return None

    def _save_alert(self, alert: EvolutionAlert):
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    """INSERT INTO evolution_alerts
                       (entity, alert_type, detail, wiki_page, severity, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (alert.entity, alert.alert_type, alert.detail,
                     alert.wiki_page, alert.severity, alert.created_at),
                )
                conn.commit()
        except Exception:
            pass

    def get_unresolved_alerts(self) -> List[EvolutionAlert]:
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT entity, alert_type, detail, wiki_page, severity, created_at "
                    "FROM evolution_alerts WHERE resolved = 0 "
                    "ORDER BY severity DESC LIMIT 20",
                )
                return [
                    EvolutionAlert(
                        entity=row[0], alert_type=row[1], detail=row[2],
                        wiki_page=row[3], severity=row[4], created_at=row[5],
                    )
                    for row in cursor
                ]
        except Exception:
            return []

    def resolve_alert(self, entity: str, alert_type: str):
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    "UPDATE evolution_alerts SET resolved = 1 "
                    "WHERE entity = ? AND alert_type = ?",
                    (entity, alert_type),
                )
                conn.commit()
        except Exception:
            pass


# ========== DarkKnowledgeIntegration ==========

class DarkKnowledgeIntegration:
    """暗知识集成 — erebus 差距 / 演变 / 关联驱动策略调整

    暗知识：用户未意识到的知识缺口，通过 erebus（影子页面）和知识图谱差距检测发现。
    """

    def enhance_distill_strategy(self, session_text: str,
                                  existing_pages: List[Path] = None) -> Dict:
        """基于暗知识分析，增强蒸馏策略

        Returns:
            {
                "strategy_adjustments": [...],
                "gap_topics": [...],
                "priority_boost": float,
            }
        """
        adjustments = []
        gap_topics = []
        priority_boost = 1.0

        # 1. 检测知识缺口（session 中出现但 wiki 中未覆盖的实体）
        session_entities = self._extract_entities(session_text)
        wiki_entities = set()
        if existing_pages:
            for page in existing_pages:
                wiki_entities.update(self._extract_entities_from_page(page))

        gaps = session_entities - wiki_entities
        if gaps:
            gap_topics = list(gaps)[:5]
            adjustments.append({
                "type": "knowledge_gap",
                "entities": gap_topics,
                "action": "优先提取这些实体的知识",
            })
            priority_boost = min(1.5, 1.0 + len(gaps) * 0.1)

        # 2. 演变信号检测（session 中提到升级/迁移/重构）
        evolution_signals = self._detect_evolution_signals(session_text)
        if evolution_signals:
            adjustments.append({
                "type": "evolution_signal",
                "signals": evolution_signals,
                "action": "标记为版本绑定知识，增加时效性检查",
            })

        # 3. 关联缺失检测（提及的实体之间缺少 wiki 链接）
        if session_entities and wiki_entities:
            overlap = session_entities & wiki_entities
            if len(overlap) < len(session_entities) * 0.3:
                adjustments.append({
                    "type": "weak_association",
                    "action": "新知识与已有知识关联度低，考虑建立链接",
                })

        return {
            "strategy_adjustments": adjustments,
            "gap_topics": gap_topics,
            "priority_boost": priority_boost,
        }

    @staticmethod
    def _extract_entities(text: str) -> set:
        entities = set()
        entities.update(w.lower() for w in re.findall(r'[a-zA-Z_]{3,}', text))
        entities.update(re.findall(r'[一-龥]{2,4}', text))
        return entities

    @staticmethod
    def _extract_entities_from_page(page: Path) -> set:
        try:
            content = page.read_text(encoding="utf-8")[:1000]
            entities = set()
            entities.update(w.lower() for w in re.findall(r'[a-zA-Z_]{3,}', content))
            entities.update(re.findall(r'[一-龥]{2,4}', content))
            return entities
        except Exception:
            return set()

    @staticmethod
    def _detect_evolution_signals(text: str) -> List[str]:
        signals = []
        patterns = [
            (r'(升级|迁移|重构|更新|deprecated).*?(\d+\.\d+)', "版本演变"),
            (r'(替代|取代|代替).+?(?:了|为)', "技术替代"),
            (r'(不再|废弃|弃用|deprecated)', "功能废弃"),
        ]
        for pattern, label in patterns:
            if re.search(pattern, text, re.I):
                signals.append(label)
        return signals


# ========== RecirculationGuard ==========

class RecirculationGuard:
    """回流防护 — 防止 Wiki 引用内容再次蒸馏

    检测机制：
    1. skip-distill=true 标签
    2. <wiki-context> 标记
    3. 已蒸馏内容的特征指纹
    """

    # Wiki 引用标记
    _WIKI_MARKERS = [
        "<wiki-context",
        "</wiki-context>",
        "<!-- wiki-injected",
        "skip-distill=true",
        "<!-- auto-maintained",
    ]

    # 已蒸馏页面的特征
    _DISTILLED_PATTERNS = [
        r'^类型:\s*\w+',           # frontmatter 中的类型字段
        r'^来源会话:\s*\w{8}',     # frontmatter 中的来源会话
        r'^证据级别:\s*\w+',       # 证据级别
        r'## 演化历史',            # 标准章节
    ]

    def should_skip(self, content: str) -> Tuple[bool, str]:
        """判断内容是否应跳过蒸馏

        Returns:
            (should_skip, reason)
        """
        if not content:
            return True, "空内容"

        # 1. Wiki 引用标记检测
        for marker in self._WIKI_MARKERS:
            if marker in content:
                return True, f"检测到 Wiki 引用标记: {marker}"

        # 2. 完整 Wiki 页面格式检测（frontmatter + 标准章节）
        has_frontmatter = content.strip().startswith("---")
        has_evolution = "## 演化历史" in content
        if has_frontmatter and has_evolution:
            return True, "内容已经是完整 Wiki 页面"

        # 3. skip-distill 标签
        if "skip-distill" in content:
            return True, "包含 skip-distill 标记"

        # 4. 内容与已有蒸馏结果高度相似
        # （此检查由 SyncEngine 在标签组装阶段完成，此处仅做基本检查）

        return False, ""

    def check_session(self, messages: List[Dict]) -> Tuple[bool, str]:
        """检查会话是否包含回流内容

        Returns:
            (has_recirculation, detail)
        """
        for msg in messages:
            content = msg.get("content", "")
            should, reason = self.should_skip(content)
            if should:
                return True, f"消息包含回流内容: {reason}"
        return False, ""
