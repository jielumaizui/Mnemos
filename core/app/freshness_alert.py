# -*- coding: utf-8 -*-
"""
Knowledge freshness — application-layer entity and ACL adapter.

The unified reminder engine owns freshness scoring. This module:
- FreshnessAlertChecker.check_knowledge_freshness() 解析实体/页面后，
  委托给 core.kia.reminder_engine.ReminderEngine.check_freshness()
- 返回应用层 FreshnessResult。

真正负责新鲜度检查的单一实现是 core.kia.reminder_engine.ReminderEngine。
新代码应直接使用 ReminderEngine。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from core.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class FreshnessResult:
    """知识新鲜度检查结果（含 not_found / error 状态）"""

    status: str  # fresh | stale | not_found | access_denied | error
    message: str
    entity_name: str = ""
    alert_type: str = ""  # version_outdated | context_expired
    confidence: float = 0.0
    current_version: str = ""
    latest_version: str = ""
    page_path: str = ""


class FreshnessAlertChecker:
    """知识新鲜度检查器"""

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            self.wiki_base = get_config().wiki_dir
        self._reminder_engine: Optional[Any] = None

    def check_knowledge_freshness(
        self,
        entity_name: str,
        *,
        candidate_filter: Callable[[Dict[str, Any]], bool] | None = None,
    ) -> Optional[FreshnessResult]:
        """
        检查特定实体的知识新鲜度。

        返回 FreshnessResult，status 取值：
        - fresh: 知识新鲜
        - stale: 知识过期（含 version_outdated / context_expired）
        - not_found: 实体不存在
        - error: 检查过程异常

        搜索附加型：只在用户搜索时展示，不主动弹出。

        实现：通过 EntityManager 解析实体到页面字典，再委托给
        ReminderEngine.check_freshness()。
        """
        try:
            from core.kia.entity_manager import EntityManager

            cfg = get_config()
            configured_wiki = Path(cfg.wiki_dir).expanduser().resolve(strict=False)
            current_wiki = self.wiki_base.expanduser().resolve(strict=False)
            db_path = (
                Path(cfg.database_dir) / "knowledge_graph.db"
                if current_wiki == configured_wiki
                else current_wiki / ".kg" / "knowledge_graph.db"
            )
            em = EntityManager(db_path=db_path)

            entity = self._resolve_entity(em, entity_name)
            if not entity:
                return FreshnessResult(
                    status="not_found",
                    entity_name=entity_name,
                    message=f"知识库中未找到「{entity_name}」，无法判断新鲜度",
                )

            page = self._entity_to_page(entity)
            if not page:
                # 实体存在但无法解析为可检查页面（无 source_page、无文件、无元数据）
                return FreshnessResult(
                    status="not_found",
                    entity_name=entity_name,
                    message=f"「{entity_name}」在知识库中存在但缺少可检查页面/元数据",
                )
            if candidate_filter is not None and not candidate_filter(page):
                return FreshnessResult(
                    status="access_denied",
                    entity_name=entity_name,
                    message="知识页面不在当前 principal 的授权范围内",
                )

            # P1-10：只有授权访问才更新实体质量/置信度。
            try:
                from core.kia.kg_event_handler import KGEventHandler

                KGEventHandler(
                    db_path=db_path,
                    wiki_base=current_wiki,
                    embedding_index_dir=db_path.parent / "embedding_index",
                ).on_entity_accessed(entity_name)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.debug("实体访问事件发射失败 %s", entity_name, exc_info=True)

            reminders = self._get_reminder_engine().check_freshness(page)

            if reminders:
                reminder = reminders[0]
                alert_type = self._reminder_type_to_alert_type(reminder.reason)
                result = FreshnessResult(
                    status="stale",
                    entity_name=entity_name,
                    alert_type=alert_type,
                    message=reminder.message,
                    confidence=reminder.confidence,
                    page_path=page.get("path", ""),
                )
                # 对 version_outdated 类型，尝试从页面 frontmatter 补全版本字段
                if alert_type == "version_outdated":
                    self._fill_version_fields(result, page)
                return result

        except (ImportError, OSError, ValueError) as e:
            logger.debug("新鲜度检查失败 %s: %s", entity_name, e, exc_info=True)
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

    def _resolve_entity(self, em, entity_name: str):
        """按 uid → name → alias 多级解析实体。"""
        entity = em.get_entity(entity_name)
        if entity:
            return entity
        # 尝试按名称查找
        if hasattr(em, "get_entity_by_name"):
            entity = em.get_entity_by_name(entity_name)
            if entity:
                return entity
        # 尝试别名解析
        if hasattr(em, "resolve_alias"):
            entity = em.resolve_alias(entity_name)
            if entity:
                return entity
        return None

    def _get_reminder_engine(self):
        """缓存 ReminderEngine 实例，避免重复初始化 cooldown DB。"""
        if self._reminder_engine is None:
            from core.kia.reminder_engine import ReminderEngine

            self._reminder_engine = ReminderEngine(wiki_base=str(self.wiki_base))
        return self._reminder_engine

    def _fill_version_fields(self, result: FreshnessResult, page: Dict) -> None:
        """从页面 frontmatter 读取 current_version / latest_version。"""
        fm = page.get("frontmatter", {}) or {}
        version_info = fm.get("version_info") or {}
        if isinstance(version_info, dict):
            result.current_version = str(
                version_info.get("current_version", result.current_version)
            )
            result.latest_version = str(version_info.get("latest_version", result.latest_version))
        result.current_version = result.current_version or str(fm.get("current_version", ""))
        result.latest_version = result.latest_version or str(fm.get("latest_version", ""))

    def _entity_to_page(self, entity) -> Optional[Dict]:
        """将 EntityManager 返回的实体解析为 ReminderEngine 可接受的页面字典。"""
        name = getattr(entity, "name", "")
        page_path = getattr(entity, "source_page", "") or getattr(entity, "wiki_page", "")

        # 从实体元数据构建 frontmatter，确保 last_updated / version_info 不丢失
        fm: Dict = {}
        last_updated = getattr(entity, "last_updated", "")
        if last_updated:
            fm["updated_at"] = last_updated
        version_info = getattr(entity, "version_info", None)
        if version_info:
            fm["version_info"] = version_info

        if page_path:
            full_path = self.wiki_base / page_path
            if full_path.exists():
                from core.frontmatter import read_frontmatter_only

                page_fm = read_frontmatter_only(full_path, errors="ignore")
                page_fm.update(fm)
                return {
                    "path": str(full_path),
                    "title": name or full_path.stem,
                    "frontmatter": page_fm,
                }
            # source_page 存在但文件不存在时仍返回合成页面，保留实体元数据
            return {
                "path": str(full_path),
                "title": name,
                "frontmatter": fm,
            }

        # 回退：按实体名查找页面
        for md_file in self.wiki_base.rglob("*.md"):
            if name.lower() in md_file.stem.lower():
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                page_fm = self._extract_frontmatter(content)
                page_fm.update(fm)
                return {
                    "path": str(md_file),
                    "title": name or md_file.stem,
                    "frontmatter": page_fm,
                }

        # 找不到对应页面时返回仅含实体元数据的合成页面
        if fm:
            return {
                "path": f"entity://{name}",
                "title": name,
                "frontmatter": fm,
            }
        return None

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        if not content.startswith("---"):
            return {}
        end = content.find("---", 3)
        if end == -1:
            return {}
        try:
            return yaml.safe_load(content[3:end]) or {}
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
        ):
            return {}

    @staticmethod
    def _reminder_type_to_alert_type(reason: str) -> str:
        if "newer_version" in reason:
            return "version_outdated"
        if "potentially_stale" in reason:
            return "context_expired"
        return "context_expired"

    def scan_all_freshness(self) -> List[FreshnessResult]:
        """扫描所有实体的新鲜度（每日批处理用）

        现委托给 ReminderEngine.scan_all_freshness()。
        """
        alerts = []
        try:
            reminders = self._get_reminder_engine().scan_all_freshness()[:100]  # 限制扫描量
            for reminder in reminders:
                alerts.append(
                    FreshnessResult(
                        status="stale",
                        entity_name=reminder.title,
                        alert_type=self._reminder_type_to_alert_type(reminder.reason),
                        message=reminder.message,
                        confidence=reminder.confidence,
                    )
                )
        except (ImportError, OSError, ValueError) as e:
            logger.warning("批量新鲜度扫描失败: %s", e, exc_info=True)

        return alerts
