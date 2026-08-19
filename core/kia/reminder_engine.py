# -*- coding: utf-8 -*-
"""
ReminderEngine — 统一提醒引擎（KIA 层）

合并原双轨实现：
- core.kia.teiresias.PredictivePushEngine（上下文预测推送）
- core.kia.proteus.KnowledgeFreshnessChecker（知识新鲜度检查）

向上为应用层提供统一入口：
- contextual_reminders(user_input)
- check_freshness(page_or_entity)
- scan_all_freshness()

应用层入口（core.app.freshness_alert）负责实体解析和 ACL 适配。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import logging
import sqlite3

from core.config import get_config

# Constants extracted from magic numbers
COOLDOWN_SECONDS = 600
COOLDOWN_SECONDS_2 = 86400

logger = logging.getLogger(__name__)


@dataclass
class Reminder:
    """统一提醒对象"""

    reminder_type: str  # "contextual" | "freshness" | "combined"
    page_path: str
    title: str
    message: str
    reason: str
    confidence: float
    priority: str  # "high" | "medium" | "low"
    action: str = ""  # optional call-to-action


class ReminderEngine:
    """统一提醒引擎：整合上下文推送与知识新鲜度检查。"""

    PRIORITY_ORDER = {"high": 3, "medium": 2, "low": 1}

    def __init__(self, wiki_base: Optional[str] = None, db_path: Optional[str] = None):
        self.cfg = get_config()
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else self.cfg.wiki_dir

        if db_path:
            self.db_path = Path(db_path).expanduser()
        else:
            self.db_path = self.cfg.database_dir / "reminder_cooldown.db"
        self._cooldown_initialized = False

        # 内部引擎懒加载
        self._push_engine: Optional[Any] = None
        self._freshness_checker: Optional[Any] = None

    # ─────────────────────────────────────────────
    # 公共 API
    # ─────────────────────────────────────────────

    def contextual_reminders(
        self,
        user_input: str,
        recent_context: Optional[str] = None,
        candidate_filter: Callable[[Reminder], bool] | None = None,
        candidate_path_filter: Callable[[str], bool] | None = None,
    ) -> List[Reminder]:
        """基于用户输入返回上下文提醒列表。"""
        if not self.cfg.get("reminder.enabled", True):
            return []

        engine = self._get_push_engine()
        decision = engine.decide_push(
            user_input,
            current_task=recent_context or "",
            candidate_path_filter=candidate_path_filter,
        )

        if not decision.should_push:
            return []

        reminders: List[Reminder] = []
        for match in decision.matches[: self.cfg.get("reminder.max_contextual_per_turn", 3)]:
            page_path = match.page_path
            reminder = Reminder(
                reminder_type="contextual",
                page_path=page_path,
                title=match.page_title,
                message=match.relevant_excerpt or f"推荐查看：{match.page_title}",
                reason=match.match_reason or decision.reason,
                confidence=min(match.match_score, 1.0),
                priority=match.push_priority,
                action="查看详情",
            )
            if candidate_filter is not None and not candidate_filter(reminder):
                continue
            if self._is_in_cooldown(page_path, "contextual"):
                continue
            reminders.append(reminder)
            self._record_shown(page_path, "contextual")

        return reminders

    def check_freshness(self, page_or_entity: Union[str, Dict]) -> List[Reminder]:
        """检查单个页面/实体的新鲜度并返回提醒列表。

        Args:
            page_or_entity: 已加载的页面字典（含 frontmatter + path），
                            或实体名称字符串（建议由应用层解析后再传入字典）。
        """
        if not self.cfg.get("reminder.enabled", True):
            return []

        checker = self._get_freshness_checker()

        if isinstance(page_or_entity, dict):
            page = page_or_entity
        else:
            # 字符串：尝试按名称查找页面，但不复用 EntityManager 逻辑
            page = self._resolve_entity_to_page(str(page_or_entity))  # type: ignore[assignment]

        if not page:
            return []

        alert = checker.check(page)
        if not alert:
            return []

        page_path = page.get("path", "")
        if self._is_in_cooldown(page_path, "freshness"):
            return []

        priority = self._freshness_priority(alert.severity)
        title = page.get("title") or Path(page_path).stem or "未知页面"
        reminder = Reminder(
            reminder_type="freshness",
            page_path=page_path,
            title=title,
            message=alert.message,
            reason=f"新鲜度检查：{alert.type}",
            confidence=self._freshness_confidence(alert.severity),
            priority=priority,
            action=alert.action or "确认有效性",
        )
        self._record_shown(page_path, "freshness")
        return [reminder]

    def scan_all_freshness(self) -> List[Reminder]:
        """扫描整个 wiki，返回所有过期页面的新鲜度提醒。"""
        if not self.cfg.get("reminder.enabled", True):
            return []

        self._get_freshness_checker()
        reminders: List[Reminder] = []

        if not self.wiki_base.exists():
            return reminders

        for md_file in self.wiki_base.rglob("*.md"):
            rel = md_file.relative_to(self.wiki_base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if md_file.name.endswith(".shadow.md"):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
                page = {
                    "path": str(md_file),
                    "title": self._extract_title(content) or md_file.stem,
                    "frontmatter": fm,
                }
                reminders.extend(self.check_freshness(page))
            except (OSError, UnicodeError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.warning("扫描新鲜度失败 %s: %s", md_file, e)
                continue

        return reminders

    def reminders_for(
        self, user_input: str, page_or_entity: Union[str, Dict, None] = None
    ) -> List[Reminder]:
        """统一入口：返回上下文 + 新鲜度提醒，并做同页去重合并。"""
        contextual = self.contextual_reminders(user_input)
        freshness: List[Reminder] = []
        if page_or_entity:
            freshness = self.check_freshness(page_or_entity)
        return self._deduplicate(contextual + freshness)

    # ─────────────────────────────────────────────
    # 冷却层（SQLite）
    # ─────────────────────────────────────────────

    def _init_cooldown_db(self):
        schema = """
            CREATE TABLE IF NOT EXISTS cooldowns (
                page_path TEXT NOT NULL,
                reminder_type TEXT NOT NULL,
                last_shown TEXT NOT NULL,
                shown_count INTEGER DEFAULT 1,
                PRIMARY KEY (page_path, reminder_type)
            );
        """
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(schema)
        self._cooldown_initialized = True

    def _ensure_cooldown_db(self) -> None:
        if self._cooldown_initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_cooldown_db()

    def _is_in_cooldown(self, page_path: str, reminder_type: str) -> bool:
        """检查指定页面/提醒类型是否在统一冷却期内。"""
        self._ensure_cooldown_db()
        if reminder_type == "contextual":
            cooldown_seconds = self.cfg.get(
                "reminder.contextual_cooldown_seconds", COOLDOWN_SECONDS
            )
        elif reminder_type == "freshness":
            cooldown_seconds = self.cfg.get(
                "reminder.freshness_cooldown_seconds", COOLDOWN_SECONDS_2
            )
        else:
            cooldown_seconds = COOLDOWN_SECONDS

        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                row = conn.execute(
                    "SELECT last_shown FROM cooldowns WHERE page_path = ? AND reminder_type = ?",
                    (page_path, reminder_type),
                ).fetchone()
                if not row:
                    return False
                last = datetime.fromisoformat(row[0])
                # type: ignore[no-any-return]
                return (datetime.now() - last).total_seconds() < cooldown_seconds  # type: ignore[no-any-return]  # noqa: E501
        except (OSError, ValueError, TypeError, sqlite3.Error) as e:
            logger.warning("冷却检查失败 %s/%s: %s", page_path, reminder_type, e)
            return False

    def _record_shown(self, page_path: str, reminder_type: str):
        """更新冷却数据库中的展示记录。"""
        self._ensure_cooldown_db()
        now = datetime.now().isoformat()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """INSERT INTO cooldowns (page_path, reminder_type, last_shown, shown_count)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT(page_path, reminder_type) DO UPDATE SET
                           last_shown = excluded.last_shown,
                           shown_count = cooldowns.shown_count + 1""",
                    (page_path, reminder_type, now),
                )
        except (OSError, ValueError, TypeError, sqlite3.Error) as e:
            logger.warning("冷却记录失败 %s/%s: %s", page_path, reminder_type, e)

    # ─────────────────────────────────────────────
    # 内部辅助
    # ─────────────────────────────────────────────

    def _get_push_engine(self):
        if self._push_engine is None:
            from core.kia.teiresias import PredictivePushEngine

            self._push_engine = PredictivePushEngine(wiki_base=str(self.wiki_base))
        return self._push_engine

    def _get_freshness_checker(self):
        if self._freshness_checker is None:
            from core.kia.proteus import KnowledgeFreshnessChecker

            self._freshness_checker = KnowledgeFreshnessChecker()
        return self._freshness_checker

    def _resolve_entity_to_page(self, entity_name: str) -> Optional[Dict]:
        """字符串实体名到页面字典的简单解析（用于不依赖 EntityManager 的场景）。"""
        try:
            from core.kia.knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            pages = kg.find_entity_pages(entity_name)  # type: ignore[attr-defined]
            if pages:
                page_path = self.wiki_base / pages[0]
                if page_path.exists():
                    content = page_path.read_text(encoding="utf-8", errors="ignore")
                    return {
                        "path": str(page_path),
                        "title": self._extract_title(content) or page_path.stem,
                        "frontmatter": self._extract_frontmatter(content),
                    }
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("实体到页面解析失败: %s", entity_name, exc_info=True)

        # 回退：按文件名精确匹配
        for md_file in self.wiki_base.rglob("*.md"):
            if entity_name.lower() in md_file.stem.lower():
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                return {
                    "path": str(md_file),
                    "title": self._extract_title(content) or md_file.stem,
                    "frontmatter": self._extract_frontmatter(content),
                }
        return {"path": f"entity://{entity_name}", "title": entity_name, "frontmatter": {}}

    def _deduplicate(self, reminders: List[Reminder]) -> List[Reminder]:
        """同 page_path 同时存在 contextual 和 freshness 时合并为 combined。"""
        by_path: Dict[str, List[Reminder]] = {}
        for r in reminders:
            by_path.setdefault(r.page_path, []).append(r)

        result: List[Reminder] = []
        for path, items in by_path.items():
            if len(items) == 1:
                result.append(items[0])
                continue

            types = {r.reminder_type for r in items}
            if {"contextual", "freshness"}.issubset(types):
                ctx = next(r for r in items if r.reminder_type == "contextual")
                fresh = next(r for r in items if r.reminder_type == "freshness")
                priority = self._higher_priority(ctx.priority, fresh.priority)
                result.append(
                    Reminder(
                        reminder_type="combined",
                        page_path=path,
                        title=ctx.title or fresh.title,
                        message=f"{ctx.message}\n—\n{fresh.message}",
                        reason=f"{ctx.reason}; {fresh.reason}",
                        confidence=max(ctx.confidence, fresh.confidence),
                        priority=priority,
                        action=fresh.action or ctx.action,
                    )
                )
            else:
                # 同类型重复：保留置信度最高的一条
                items.sort(key=lambda r: r.confidence, reverse=True)
                result.append(items[0])

        result.sort(
            key=lambda r: (self.PRIORITY_ORDER.get(r.priority, 0), r.confidence), reverse=True
        )
        return result

    @classmethod
    def _higher_priority(cls, a: str, b: str) -> str:
        return a if cls.PRIORITY_ORDER.get(a, 0) >= cls.PRIORITY_ORDER.get(b, 0) else b

    @staticmethod
    def _freshness_priority(severity: str) -> str:
        return {"high": "high", "medium": "medium"}.get(severity, "low")

    @staticmethod
    def _freshness_confidence(severity: str) -> float:
        return {"high": 0.85, "medium": 0.65}.get(severity, 0.45)

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict[str, Any]:
        if not content.startswith("---"):
            return {}
        end = content.find("---", 3)
        if end == -1:
            return {}
        try:
            import yaml

            return yaml.safe_load(content[3:end]) or {}
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            return {}

    @staticmethod
    def _extract_title(content: str) -> str:
        import re

        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""
