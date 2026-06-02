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
    """知识过时提醒（向后兼容）"""
    entity_name: str
    alert_type: str  # version_outdated / context_expired / rarely_accessed
    message: str
    confidence: float
    current_version: str = ""
    latest_version: str = ""


@dataclass
class FreshnessResult:
    """知识新鲜度检查结果（含 not_found / error 状态）"""
    status: str  # fresh | stale | not_found | error
    message: str
    entity_name: str = ""
    alert_type: str = ""  # version_outdated | context_expired | rarely_accessed
    confidence: float = 0.0
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

    def check_knowledge_freshness(self, entity_name: str) -> Optional[FreshnessResult]:
        """
        检查特定实体的知识新鲜度。

        返回 FreshnessResult，status 取值：
        - fresh: 知识新鲜
        - stale: 知识过期（含 version_outdated / context_expired / rarely_accessed）
        - not_found: 实体不存在
        - error: 检查过程异常

        搜索附加型：只在用户搜索时展示，不主动弹出。
        """
        try:
            from core.kia.entity_manager import EntityManager
            em = EntityManager()

            entity = em.get_entity(entity_name)
            if not entity:
                return FreshnessResult(
                    status="not_found",
                    entity_name=entity_name,
                    message=f"知识库中未找到「{entity_name}」，无法判断新鲜度",
                )

            # 版本绑定检查
            if entity.entity_type in ("technology", "tool", "framework"):
                alert = self._check_version_bound(entity)
                if alert:
                    return FreshnessResult(
                        status="stale",
                        entity_name=alert.entity_name,
                        alert_type=alert.alert_type,
                        message=alert.message,
                        confidence=alert.confidence,
                        current_version=alert.current_version,
                        latest_version=alert.latest_version,
                    )

            # 上下文知识过期检查
            alert = self._check_context_expiry(entity)
            if alert:
                return FreshnessResult(
                    status="stale",
                    entity_name=alert.entity_name,
                    alert_type=alert.alert_type,
                    message=alert.message,
                    confidence=alert.confidence,
                )

            # 罕见访问检查
            alert = self._check_rarely_accessed(entity)
            if alert:
                return FreshnessResult(
                    status="stale",
                    entity_name=alert.entity_name,
                    alert_type=alert.alert_type,
                    message=alert.message,
                    confidence=alert.confidence,
                )

        except Exception as e:
            logger.debug(f"新鲜度检查失败 {entity_name}: {e}")
            return FreshnessResult(
                status="error",
                entity_name=entity_name,
                message=f"新鲜度检查异常: {e}",
            )

        return FreshnessResult(
            status="fresh",
            entity_name=entity_name,
            message=f"「{entity_name}」知识新鲜",
        )

    def scan_all_freshness(self) -> List[FreshnessResult]:
        """扫描所有实体的新鲜度（每日批处理用）"""
        alerts = []
        try:
            from core.kia.entity_manager import EntityManager
            em = EntityManager()
            entities = em.get_all_entities()

            for entity in entities[:100]:  # 限制扫描量
                result = self.check_knowledge_freshness(entity.name)
                if result and result.status == "stale":
                    alerts.append(result)
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
            # 使用 entity.last_updated（Entity dataclass 正确字段）
            updated = getattr(entity, "last_updated", "")
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
