# -*- coding: utf-8 -*-
"""
FreshnessAlert — 知识演化提醒

版本绑定检查，90 天上下文知识过时警告。
输出为搜索附加型（不主动弹出）。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional



logger = logging.getLogger(__name__)
@dataclass
class FreshnessAlert:
    """知识过时提醒"""
    entity_name: str
    alert_type: str  # version_outdated / context_expired / rarely_accessed
    message: str
    confidence: float
    current_version: str = ""
    latest_version: str = ""


class FreshnessAlertChecker:
    """知识新鲜度检查器"""

    CONTEXT_EXPIRY_DAYS = 90  # 上下文知识默认过期天数
    RARELY_ACCESSED_DAYS = 60  # 60 天无访问视为罕见访问

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            from core.config import get_config
            self.wiki_base = get_config().wiki_dir

    def check_knowledge_freshness(self, entity_name: str) -> Optional[FreshnessAlert]:
        """
        检查特定实体的知识新鲜度。

        搜索附加型：只在用户搜索时展示，不主动弹出。
        """
        try:
            from core.kia.entity_manager import EntityManager
            em = EntityManager()

            entity = em.get_entity(entity_name)
            if not entity:
                return None

            # 版本绑定检查
            if entity.entity_type in ("technology", "tool", "framework"):
                alert = self._check_version_bound(entity)
                if alert:
                    return alert

            # 上下文知识过期检查
            alert = self._check_context_expiry(entity)
            if alert:
                return alert

            # 罕见访问检查
            alert = self._check_rarely_accessed(entity)
            if alert:
                return alert

        except Exception as e:
            logger.debug(f"新鲜度检查失败 {entity_name}: {e}")

        return None

    def scan_all_freshness(self) -> List[FreshnessAlert]:
        """扫描所有实体的新鲜度（每日批处理用）"""
        alerts = []
        try:
            from core.kia.entity_manager import EntityManager
            em = EntityManager()
            entities = em.get_all_entities()

            for entity in entities[:100]:  # 限制扫描量
                alert = self.check_knowledge_freshness(entity.name)
                if alert:
                    alerts.append(alert)
        except Exception as e:
            logger.warning(f"批量新鲜度扫描失败: {e}")

        return alerts

    def _check_version_bound(self, entity) -> Optional[FreshnessAlert]:
        """检查版本绑定的知识是否过时"""
        # 从实体元数据获取版本信息
        try:
            from core.kia.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))

            # 查找实体关联的 Wiki 页面
            pages = kg.find_entity_pages(entity.name)
            for page in pages:
                page_path = self.wiki_base / page
                if not page_path.exists():
                    continue

                content = page_path.read_text(encoding="utf-8", errors="ignore")
                # 检查 frontmatter 中的版本信息
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            import yaml
                            fm = yaml.safe_load(parts[1]) or {}
                            version_info = fm.get("version", "")
                            last_verified = fm.get("last_verified", "")

                            if version_info and last_verified:
                                verified_date = datetime.fromisoformat(last_verified.replace("Z", "+00:00"))
                                days_since = (datetime.now() - verified_date).days

                                if days_since > 365:
                                    return FreshnessAlert(
                                        entity_name=entity.name,
                                        alert_type="version_outdated",
                                        message=f"「{entity.name}」的知识基于 {version_info}，已 {days_since} 天未验证",
                                        confidence=0.8,
                                        current_version=version_info,
                                    )
                        except Exception:
                            logging.getLogger(__name__).warning(f"Caught unexpected error at freshness_alert.py", exc_info=True)
                            continue
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
        return None

    def _check_context_expiry(self, entity) -> Optional[FreshnessAlert]:
        """检查上下文知识是否过期"""
        try:
            # NOTE: TemporalEvolutionTracker 未实现，使用 EntityManager 的更新时间来判断
            from core.kia.entity_manager import EntityManager
            em = EntityManager()
            updated = entity.meta.get("updated_at", "")
            if updated:
                from datetime import datetime
                try:
                    last_update = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    days_since = (datetime.now() - last_update).days
                    if days_since >= self.CONTEXT_EXPIRY_DAYS:
                        return FreshnessAlert(
                            entity_name=entity.name,
                            alert_type="context_expired",
                            message=f"「{entity.name}」的上下文知识可能已过时（{days_since}天未更新）",
                            confidence=0.6,
                        )
                except ValueError:
                    pass
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
        return None

    def _check_rarely_accessed(self, entity) -> Optional[FreshnessAlert]:
        """检查是否罕见访问（可能暗示过时）"""
        try:
            from core.kia.context_query import ContextAwareQuery
            import sqlite3
            from core.config import get_config

            db_path = get_config().data_dir / "context_query.db"
            if not db_path.exists():
                return None

            cutoff = (datetime.now() - timedelta(days=self.RARELY_ACCESSED_DAYS)).isoformat()
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM query_logs WHERE entity_name = ? AND timestamp >= ?",
                    (entity.name, cutoff),
                )
                access_count = cursor.fetchone()[0]

                if access_count == 0:
                    return FreshnessAlert(
                        entity_name=entity.name,
                        alert_type="rarely_accessed",
                        message=f"「{entity.name}」已 {self.RARELY_ACCESSED_DAYS} 天未被查询",
                        confidence=0.4,
                    )
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
        return None
