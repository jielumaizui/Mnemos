"""
Pre-flight Injector - 预加载注入器

从 wiki/06-Retrospectives/ 装载历史经验，根据时间窗口决定策略：
- 即时/短期：直接装载完整清单
- 中期/长期：不装载，记入调度器
- 周期性：检查上次执行，自动装载

支持：
1. 知识衰减（freshness_score 排序）
2. 场景适配（applies_when/not_applies_when 过滤）
3. 命中追踪（hit_count/last_hit）
4. 相关性排序（高 hit 优先）
"""

# Prophasis — 预显/预演 — 任务前知识装载，KIA 第一步
# 原模块: pre_flight_injector.py


import re
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .kairos import TimeWindow, TimeWindowType
from core.persona.delphi import PersonaStore
from core.config import get_config
from core.frontmatter import fm_get


import logging

# Constants extracted from magic numbers
PRE_FLIGHT_INJECTOR_DURATION_BUCKET_WEEK_DAYS = 7
PRE_FLIGHT_INJECTOR_DURATION_BUCKET_MONTH_DAYS = 30
logger = logging.getLogger(__name__)


@dataclass
class ChecklistItem:
    """校验清单项"""

    item: str
    source: str  # 来源版本
    severity: str = "medium"  # critical/high/medium/low
    freshness_score: float = 1.0  # 新鲜度 0-1
    hit_count: int = 0  # 历史命中次数
    ignore_count: int = 0  # 被忽略次数（负样本学习）
    ignore_reasons: List[str] = field(default_factory=list)  # 忽略原因记录
    last_hit: Optional[str] = None  # 上次命中时间
    last_ignore: Optional[str] = None  # 上次忽略时间
    applies_when: List[str] = field(default_factory=list)
    not_applies_when: List[str] = field(default_factory=list)
    trigger_keywords: List[str] = field(default_factory=list)
    risk_patterns: List[str] = field(default_factory=list)
    detail: str = ""


# AI 行为约束清单（跨任务通用，注入所有对话）
BEHAVIOR_CONSTRAINTS = [
    ChecklistItem(
        item="同一文件/工具读取达到 2 次 → 立即停止分析，基于已有信息直接行动",
        source="anti-pattern:analysis-paralysis",
        severity="high",
        trigger_keywords=["读取", "分析", "查看", "检查"],
        risk_patterns=["重复读取", "同一文件", "反复分析"],
    ),
    ChecklistItem(
        item="发现根因后 3 句话内必须进入修复/提交/行动，不得继续深入分析",
        source="anti-pattern:analysis-paralysis",
        severity="high",
        trigger_keywords=["根因", "原因", "问题"],
        risk_patterns=["继续分析", "深入研究", "再确认一下"],
    ),
    ChecklistItem(
        item="用户明确要求'修复/修/提交/改'时，分析权重降到 10%，行动权重提到 90%",
        source="user-preference:action-first",
        severity="critical",
        trigger_keywords=["修复", "修", "提交", "改", "改一下"],
    ),
    ChecklistItem(
        item="思考记录超过 50 行但无代码/文件修改 → 触发分析瘫痪告警，立即切换为行动模式",
        source="anti-pattern:analysis-paralysis",
        severity="high",
        trigger_keywords=["思考", "分析", "考虑"],
        risk_patterns=["还在分析", "继续思考", "让我再想想"],
    ),
]


@dataclass
class LoadedKnowledge:
    """装载的知识"""

    task_type: str
    subtype: str
    version: int
    checklist: List[ChecklistItem]
    lessons_summary: str
    loaded_at: str
    is_compact: bool = False
    total_items: int = 0  # 总条目数（含未显示的）
    hit_items: int = 0  # 有命中记录的条目数
    ignored_items: int = 0  # 有被忽略记录的条目数


class PreFlightInjector:
    """预加载注入器"""

    WIKI_BASE: Optional[Path] = None
    RETROSPECTIVES_DIR: Optional[Path] = None

    # 场景标签提取模式
    SCENARIO_PATTERNS = {
        "target:price_sensitive": ["价格敏感", "低价", "优惠", "便宜", "实惠"],
        "target:vip": ["vip", "高端", "高价值", "vip客户", "大客户"],
        "target:general": ["普通用户", "大众", "全体"],
        "scale:small": ["小规模", "小范围", "内部", "20人", "30人"],
        "scale:medium": ["中等规模", "50人", "100人"],
        "scale:large": ["大规模", "千人", "万人", "全网"],
    }

    # 注入缓存 TTL（秒）：同一 (task_type, subtype) 5 分钟内不重复注入
    _INJECTION_CACHE_TTL_SEC = 300

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.WIKI_BASE = Path(wiki_base).expanduser()
        else:
            self.WIKI_BASE = get_config().wiki_dir
        self.RETROSPECTIVES_DIR = self.WIKI_BASE / "06-Retrospectives"
        try:
            self.persona_store = PersonaStore(self.WIKI_BASE)
        except (OSError, RuntimeError, ValueError):
            logger.debug("PersonaStore is unavailable; preflight continues without persona", exc_info=True)
            self.persona_store = None
        self.current_persona = None
        self._cache_db_path = get_config().database_dir / "checklist_cache.db"
        self._warm_checklist_cache()
        # 实例级注入缓存：{(task_type, subtype): timestamp}
        self._injection_cache: Dict[Tuple[str, str], datetime] = {}

    def inject(
        self, task_type: str, subtype: str, time_window: TimeWindow, context_text: str = ""
    ) -> Optional[LoadedKnowledge]:
        """
        根据时间窗口决定装载策略

        Args:
            task_type: 任务类型
            subtype: 子类型
            time_window: 时间窗口
            context_text: 当前会话上下文（用于场景适配）

        Returns:
            LoadedKnowledge 或 None
        """
        # 0. 防循环：同一 (task_type, subtype) TTL 内不重复注入
        cache_key = (task_type, subtype)
        now = datetime.now()
        last_injected = self._injection_cache.get(cache_key)
        if last_injected and (now - last_injected).total_seconds() < self._INJECTION_CACHE_TTL_SEC:
            logger.debug(
                "inject: %s/%s 已在 %.0f 秒内注入过，跳过",
                task_type,
                subtype,
                self._INJECTION_CACHE_TTL_SEC,
            )
            return LoadedKnowledge(
                task_type=task_type,
                subtype=subtype,
                version=0,
                checklist=[],
                lessons_summary="",
                loaded_at=now.isoformat(),
                is_compact=True,
                total_items=0,
                hit_items=0,
                ignored_items=0,
            )

        # 1. 加载用户画像（如果可用）
        self._load_persona()

        # 2. 根据时间窗口决定策略
        result: Optional[LoadedKnowledge] = None
        if time_window.window in (TimeWindowType.IMMEDIATE, TimeWindowType.SHORT):
            result = self._load_full(task_type, subtype, context_text)
        elif time_window.is_periodic:
            # 周期性任务：装载最新版本
            result = self._load_full(task_type, subtype, context_text)
        else:
            # 中期/长期：不装载
            result = None

        # 3. 成功注入后更新缓存（None 也记录，避免频繁检查）
        if result is not None and result.version > 0:
            self._injection_cache[cache_key] = now
        return result

    @staticmethod
    def _merge_behavior_constraints(checklist_items: List[ChecklistItem]) -> List[ChecklistItem]:
        """合并通用行为约束，并保证行为约束在截断前不会被挤掉。"""
        merged = list(BEHAVIOR_CONSTRAINTS)
        seen = {item.item for item in merged}
        for item in checklist_items:
            if item.item not in seen:
                merged.append(item)
                seen.add(item.item)
        return merged

    def _load_full(
        self, task_type: str, subtype: str, context_text: str
    ) -> Optional[LoadedKnowledge]:
        """装载完整清单（无专用复盘文件时从 Wiki 页面 fallback）"""
        # 加载知识缺口提示（若与当前上下文相关）
        knowledge_gaps = self._load_knowledge_gaps()
        matched_gaps = self._filter_relevant_gaps(knowledge_gaps, task_type, subtype, context_text)

        latest = self._find_latest_version(task_type, subtype)
        if not latest:
            # Fallback：从缓存或 Wiki 页面搜索匹配类型的页面
            checklist_items = self._get_checklist_for_type(task_type) or []
            # 回流 Layer 5 反射经验（reflection DB）
            checklist_items = self._merge_layer5_experiences(checklist_items)
            # 注入通用行为约束（防分析瘫痪）
            checklist_items = self._merge_behavior_constraints(checklist_items)
            checklist_items = self._merge_knowledge_gaps(checklist_items, matched_gaps)
            max_items = 10
            displayed_items = checklist_items[:max_items]
            return LoadedKnowledge(
                task_type=task_type,
                subtype=subtype,
                version=1,
                checklist=displayed_items,
                lessons_summary=self._build_fallback_lessons_summary(checklist_items),
                loaded_at=datetime.now().isoformat(),
                is_compact=len(checklist_items) > 10,
                total_items=len(checklist_items),
                hit_items=0,
                ignored_items=0,
            )

        frontmatter, body = self._parse_retrospective(latest)
        if not frontmatter:
            return None

        # 解析 checklist（优先 frontmatter，无则从正文生成）
        raw_checklist = frontmatter.get("checklist", [])
        if not raw_checklist:
            raw_checklist = self._generate_checklist_from_page(frontmatter, body)
        checklist_items = [self._parse_checklist_item(item) for item in raw_checklist]

        # 回流 Layer 5 反射经验（reflection DB）
        checklist_items = self._merge_layer5_experiences(checklist_items)

        # 1. 场景适配过滤
        scenario_tags = self._extract_scenario_tags(context_text)
        checklist_items = self._filter_by_scenario(checklist_items, scenario_tags)

        # 2. 合并知识缺口提示
        checklist_items = self._merge_knowledge_gaps(checklist_items, matched_gaps)

        # 3. 知识衰减排序（场景匹配度 + 热力 + 新鲜度）
        checklist_items = self._sort_by_relevance(checklist_items, scenario_tags)

        # 4. 注入通用行为约束（防分析瘫痪）
        checklist_items = self._merge_behavior_constraints(checklist_items)

        # 5. 限制数量（避免 context 超限）
        max_items = 10
        compact = len(checklist_items) > max_items
        displayed_items = checklist_items[:max_items]

        # 5. 静默装载存在感统计
        total_items = len(checklist_items)
        hit_items = sum(1 for i in checklist_items if i.hit_count > 0)
        ignored_items = sum(1 for i in checklist_items if i.ignore_count > 0)

        return LoadedKnowledge(
            task_type=task_type,
            subtype=subtype,
            version=frontmatter.get("version", 1),
            checklist=displayed_items,
            lessons_summary=frontmatter.get("lessons_summary", ""),
            loaded_at=datetime.now().isoformat(),
            is_compact=compact,
            total_items=total_items,
            hit_items=hit_items,
            ignored_items=ignored_items,
        )

    def _load_persona(self):
        """加载当前用户画像"""
        if self.persona_store is None:
            self.current_persona = None
            return
        try:
            profile, _ = self.persona_store.load_persona()
            self.current_persona = profile
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at prophasis.py", exc_info=True
            )
            self.current_persona = None

    def _load_knowledge_gaps(self) -> List[ChecklistItem]:
        """读取 EvolutionTracker 生成的知识缺口提示"""
        if not self.RETROSPECTIVES_DIR:
            return []
        gaps_file = self.RETROSPECTIVES_DIR / "knowledge_gaps.md"
        if not gaps_file.exists():
            return []
        try:
            content = gaps_file.read_text(encoding="utf-8")
        except (IOError, UnicodeDecodeError):
            return []

        items: List[ChecklistItem] = []
        entity = ""
        alert_type = ""
        detail = ""
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("## ") and not line.startswith("## 使用"):
                if entity:
                    items.append(self._build_gap_checklist_item(entity, alert_type, detail))
                entity = line[3:].strip()
                alert_type = ""
                detail = ""
            elif line.startswith("- **缺口类型**:"):
                alert_type = line.split(":", 1)[1].strip()
            elif line.startswith("- **详情**:"):
                detail = line.split(":", 1)[1].strip()
            elif line.startswith("- **建议引导方向**:") and entity:
                # 到达引导方向即可确定当前 gap 已读完
                items.append(self._build_gap_checklist_item(entity, alert_type, detail))
                entity = ""
                alert_type = ""
                detail = ""
        if entity:
            items.append(self._build_gap_checklist_item(entity, alert_type, detail))
        return items

    @staticmethod
    def _build_gap_checklist_item(entity: str, alert_type: str, detail: str) -> ChecklistItem:
        """把知识缺口封装为 checklist 项"""
        severity = "medium"
        if alert_type in ("version_outdated", "contradicted"):
            severity = "high"
        elif alert_type in ("rarely_accessed",):
            severity = "low"
        return ChecklistItem(
            item=f"知识缺口：{entity}",
            source="06-Retrospectives/knowledge_gaps.md",
            severity=severity,
            trigger_keywords=[entity],
            detail=f"类型={alert_type}; {detail}",
        )

    def _filter_relevant_gaps(
        self, gaps: List[ChecklistItem], task_type: str, subtype: str, context_text: str
    ) -> List[ChecklistItem]:
        """根据当前任务类型/上下文筛选相关的知识缺口"""
        if not gaps:
            return []
        context_lower = (context_text or "").lower()
        query_terms = {t.lower() for t in (task_type, subtype) if t}

        def _entity_keywords(entity: str) -> List[str]:
            """提取 entity 中的匹配关键词（中文按 2-gram，英文保留单词）。"""
            keywords = []
            for token in re.split(r"[^\w\u4e00-\u9fff]+", entity.lower()):
                if not token:
                    continue
                if all("\u4e00" <= ch <= "\u9fff" for ch in token):
                    # 中文连续片段：拆成 2-gram 提高容错
                    for i in range(len(token) - 1):
                        keywords.append(token[i : i + 2])
                elif len(token) > 1:
                    keywords.append(token)
            return keywords

        matched = []
        for gap in gaps:
            entity = (gap.item or "").replace("知识缺口：", "").strip()
            if not entity:
                continue
            # 完全命中上下文，或 task_type/subtype 匹配 entity
            if entity.lower() in context_lower:
                matched.append(gap)
                continue
            keywords = _entity_keywords(entity)
            if any(kw in context_lower for kw in keywords):
                matched.append(gap)
                continue
            if any(term and term in entity.lower() for term in query_terms):
                matched.append(gap)
        return matched

    def _merge_knowledge_gaps(
        self, checklist_items: List[ChecklistItem], gaps: List[ChecklistItem]
    ) -> List[ChecklistItem]:
        """将相关知识缺口合并到 checklist 前端"""
        if not gaps:
            return checklist_items
        seen = {item.item for item in checklist_items}
        merged = []
        for gap in gaps:
            if gap.item not in seen:
                merged.append(gap)
                seen.add(gap.item)
        # 缺口项放在前面，确保被注意到
        return merged + checklist_items

    def _merge_layer5_experiences(
        self, checklist_items: List[ChecklistItem]
    ) -> List[ChecklistItem]:
        """将 reflection DB 中的 Layer 5 经验合并到 checklist（去重）"""
        layer5_items = self._load_layer5_experiences()
        if not layer5_items:
            return checklist_items
        seen = {item.item for item in checklist_items}
        merged = list(checklist_items)
        for item in layer5_items:
            if item.item and item.item not in seen:
                merged.append(item)
                seen.add(item.item)
        return merged

    def _load_layer5_experiences(self, limit: int = 10) -> List[ChecklistItem]:
        """Fail closed until Layer-5 records carry object ACLs and receipts.

        The historical ``layer5_experiences`` table lacks a principal, scope,
        purpose, provenance lineage, and deletion receipt.  Reading it here
        would inject another session's inferred cognition directly into a
        preflight prompt.  A future migrated owner must expose an authorized
        retrieval API; this quarantined historical path intentionally has no
        body fallback.
        """

        del limit
        return []

    @staticmethod
    def _format_layer5_experience_item(exp: Dict) -> str:
        """把 Layer 5 经验格式化为 checklist 文本"""
        summary = (exp.get("summary") or "").strip()
        if summary:
            return summary
        reason = (exp.get("reason") or "").strip()
        if reason:
            return reason
        exp_type = exp.get("type", "")
        dimension = exp.get("dimension", "")
        from_state = exp.get("from_state", "")
        to_state = exp.get("to_state", "")
        if exp_type == "cognitive_shift" and dimension and from_state and to_state:
            return f"认知变迁：{dimension} 从 {from_state} 转向 {to_state}"
        return ""

    @staticmethod
    def _format_layer5_experience_detail(exp: Dict) -> str:
        """把 Layer 5 经验元数据格式化为详情"""
        parts: List[str] = []
        dimension = exp.get("dimension") or ", ".join(exp.get("dimensions", []))
        if dimension:
            parts.append(f"维度={dimension}")
        trigger = exp.get("trigger")
        if trigger:
            parts.append(f"触发={trigger}")
        confidence = exp.get("confidence")
        if confidence is not None:
            parts.append(f"置信={float(confidence):.2f}")
        evidence = exp.get("evidence", [])
        if evidence:
            parts.append(f"证据={evidence[:3]}")
        return "; ".join(parts)

    def _warm_checklist_cache(self):
        """遍历 04-Concepts/ 和 06-Retrospectives/，预热 checklist 缓存"""
        import sqlite3

        self._cache_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._cache_db_path))
        try:
            cursor = conn.cursor()
            self._init_checklist_cache_table(cursor)
            allowed_types = {
                "retrospective",
                "problem-solution",
                "anti-pattern",
                "methodology",
                "insight",
            }
            dirs = [
                self.WIKI_BASE / "04-Concepts",
                self.WIKI_BASE / "06-Retrospectives",
            ]
            current_paths = set()
            for d in dirs:
                if not d.exists():
                    continue
                for md_file in d.rglob("*.md"):
                    try:
                        page_path = str(md_file.relative_to(self.WIKI_BASE)).replace(
                            "\\", "/"
                        )
                        current_paths.add(page_path)
                        self._cache_single_page(
                            cursor, page_path, md_file, allowed_types
                        )
                    # DEBT(S8): 容错跳过，避免单条记录中断批量处理
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        KeyError,
                        ImportError,
                        AttributeError,
                        RuntimeError,
                    ):
                        continue
            self._prune_stale_checklist_paths(cursor, current_paths)
            conn.commit()
        finally:
            conn.close()

    def _init_checklist_cache_table(self, cursor):
        """初始化 checklist 缓存表。"""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS checklist_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT,
                keyword TEXT,
                title TEXT,
                page_path TEXT,
                source TEXT,
                created_at TEXT
            )
        """)

    def _cache_single_page(
        self, cursor, page_path: str, md_file: Path, allowed_types: set
    ):
        """解析单个页面并将其关键词写入缓存。"""
        frontmatter, body = self._parse_retrospective(md_file)
        if frontmatter is None:
            return
        page_type = fm_get(frontmatter, "type", "")
        if page_type not in allowed_types:
            return

        title = (
            fm_get(frontmatter, "name")
            or fm_get(frontmatter, "title")
            or md_file.stem
        )
        task_types = self._resolve_task_types(frontmatter, md_file)
        keywords = self._normalize_string_list(fm_get(frontmatter, "keywords", []))
        triggers = self._normalize_string_list(fm_get(frontmatter, "triggers", []))
        source = fm_get(frontmatter, "source", "") or "wiki"
        created_at = datetime.now().isoformat()

        cursor.execute(
            "DELETE FROM checklist_cache WHERE page_path = ?", (page_path,)
        )
        for tt in task_types:
            for kw in keywords + triggers:
                if not isinstance(kw, str):
                    continue
                cursor.execute(
                    "INSERT INTO checklist_cache (task_type, keyword, title, page_path, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",  # noqa: E501
                    (tt, kw, title, page_path, source, created_at),
                )

    @staticmethod
    def _resolve_task_types(frontmatter: Dict, md_file: Path) -> List[str]:
        """从 frontmatter 或文件名推断 task_type 列表。"""
        task_types = []
        applies_when = fm_get(frontmatter, "applies_when", {})
        if isinstance(applies_when, dict):
            task_types = applies_when.get("task_type", [])
        if isinstance(task_types, str):
            task_types = [task_types]
        if not task_types:
            task_types = fm_get(frontmatter, "task_type", [])
            if isinstance(task_types, str):
                task_types = [task_types]
        if not task_types:
            stem = md_file.stem
            if "反模式" in stem or "问题-解决" in stem:
                task_types = ["coding", "debugging"]
            elif "决策记录" in stem:
                task_types = ["design"]
            else:
                task_types = [""]
        return task_types

    @staticmethod
    def _normalize_string_list(value) -> List[str]:
        """将可能为字符串的 frontmatter 字段统一为列表。"""
        if isinstance(value, str):
            return [value]
        return value or []

    def _prune_stale_checklist_paths(self, cursor, current_paths: set):
        """删除缓存中已不存在的页面路径。"""
        cursor.execute("SELECT DISTINCT page_path FROM checklist_cache")
        existing_paths = {row[0] for row in cursor.fetchall()}
        for path in existing_paths - current_paths:
            cursor.execute(
                "DELETE FROM checklist_cache WHERE page_path = ?", (path,)
            )

    def _get_checklist_for_type(self, task_type: str) -> List[ChecklistItem]:
        """优先从 SQLite 缓存获取 checklist，无命中则回退到文件搜索"""
        items = []
        try:
            import sqlite3

            conn = sqlite3.connect(str(self._cache_db_path))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT keyword, title, page_path, source FROM checklist_cache WHERE task_type = ? OR task_type = ''",  # noqa: E501
                (task_type,),
            )
            rows = cursor.fetchall()
            conn.close()
            for row in rows:
                keyword, title, page_path, source = row
                label = title or Path(page_path).stem or keyword
                items.append(
                    ChecklistItem(
                        item=f"复用《{label}》：{keyword}",
                        source=page_path or source or "wiki",
                        severity="medium",
                        trigger_keywords=[keyword],
                        detail=f"source_agent={source}" if source and source != page_path else "",
                    )
                )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.warning("读取 checklist 缓存失败: %s", e)
            items = []
        if items:
            return items
        return self._get_checklist_from_files(task_type)

    def _build_fallback_lessons_summary(self, items: List[ChecklistItem]) -> str:
        """为通用 Wiki fallback 生成宿主 Agent 可读的经验摘要。"""
        if not items:
            return ""
        top_items = []
        seen = set()
        for item in items:
            text = item.item.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            top_items.append(text)
            if len(top_items) >= 5:
                break
        return (
            f"未命中专用复盘文件，已从 Wiki/Retrospectives 装载 {len(items)} 条相关检查项。"
            f"优先关注：{'；'.join(top_items)}"
        )

    def _get_checklist_from_files(self, task_type: str) -> List[ChecklistItem]:
        """回退：从 Wiki 文件搜索匹配的 checklist"""
        latest = self._find_wiki_fallback(task_type)
        if not latest:
            return []
        frontmatter, body = self._parse_retrospective(latest)
        raw_checklist = frontmatter.get("checklist", [])  # type: ignore[union-attr]
        if not raw_checklist:
            raw_checklist = self._generate_checklist_from_page(
                frontmatter, body  # type: ignore[arg-type]
            )  # type: ignore[arg-type]
        return [self._parse_checklist_item(item) for item in raw_checklist]

    def _find_latest_version(self, task_type: str, subtype: str) -> Optional[Path]:
        """查找最新版本的复盘文件"""
        # 目录结构: wiki/06-Retrospectives/{task_type}/{subtype}-v{N}.md
        # 或软链接: wiki/06-Retrospectives/{task_type}/{subtype}-active.md
        task_dir = self.RETROSPECTIVES_DIR / task_type  # type: ignore[operator]
        if not task_dir.exists():
            return None

        # 先检查 active 软链接
        active_link = task_dir / f"{subtype}-active.md"
        if active_link.exists() and active_link.is_symlink():
            resolved = active_link.resolve()
            if resolved.exists():
                return resolved

        # 否则找版本号最高的
        pattern = re.compile(re.escape(subtype) + r"-v(\d+)\.md$")
        versions = []
        for f in task_dir.glob(f"{subtype}-v*.md"):
            match = pattern.search(f.name)
            if match:
                versions.append((int(match.group(1)), f))

        if versions:
            versions.sort(reverse=True)
            return versions[0][1]

        return None

    def _parse_retrospective(self, path: Path) -> Tuple[Optional[Dict], str]:
        """解析复盘文件，返回 (frontmatter, body)"""
        try:
            content = path.read_text(encoding="utf-8")
        except (IOError, UnicodeDecodeError):
            return None, ""

        # 解析 frontmatter
        frontmatter = {}  # type: ignore[var-annotated]
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    body = parts[2].strip()
                except yaml.YAMLError:
                    logger.warning("[prophasis] yaml.YAMLError suppressed", exc_info=True)

        return frontmatter, body

    def _find_wiki_fallback(self, task_type: str) -> Optional[Path]:
        """从 06-Retrospectives 搜索匹配 task_type 的页面（frontmatter + 文件名双重匹配）"""
        retro_dir = self.WIKI_BASE / "06-Retrospectives"  # type: ignore[operator]
        if not retro_dir.exists():
            return None
        candidates = []
        for md_file in retro_dir.glob("*.md"):
            if md_file.name.startswith("_"):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                page_type = ""
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        page_type = fm.get("类型", "")

                stem = md_file.stem
                matched = False
                # 1. frontmatter 类型匹配
                if task_type.lower() in page_type.lower():
                    matched = True
                # 2. 文件名中的分类标记匹配（反模式/问题-解决/决策记录）
                elif task_type in ("coding", "debugging") and (
                    "反模式" in stem or "问题-解决" in stem
                ):
                    matched = True
                elif task_type == "design" and "决策记录" in stem:
                    matched = True
                # 3. 通用 fallback：文件名含 task_type 关键词
                elif task_type.lower() in stem.lower():
                    matched = True

                if matched:
                    candidates.append(md_file)
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                continue
        # 取最新修改的
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return candidates[0]
        return None

    def _generate_checklist_from_page(self, frontmatter: Dict, body: str) -> List[Dict]:
        """从 Wiki 页面内容生成 checklist（无专用 checklist 字段时 fallback）"""
        items = []
        # 1. 从关键词生成
        keywords = frontmatter.get("关键词", [])
        if isinstance(keywords, list):
            for kw in keywords[:5]:
                if isinstance(kw, str):
                    items.append(
                        {
                            "item": f"相关知识: {kw}",
                            "severity": "medium",
                            "trigger_keywords": [kw],
                        }
                    )
        # 2. 从触发器生成
        triggers = frontmatter.get("触发器", [])
        if isinstance(triggers, list):
            for t in triggers[:3]:
                if isinstance(t, str):
                    items.append(
                        {
                            "item": f"触发场景: {t}",
                            "severity": "low",
                            "trigger_keywords": [t],
                        }
                    )
        # 3. 从正文标题中提取反模式/缺陷/注意事项
        import re

        # 匹配 Markdown 标题中的缺陷/反模式/注意/警告
        for line in body.split("\n"):
            m = re.match(r"^#{2,4}\s+(.+)$", line.strip())
            if m:
                title = m.group(1).strip()
                # 过滤掉通用标题，保留具体项
                if any(
                    k in title for k in ("缺陷", "反模式", "注意", "警告", "风险", "坑", "问题")
                ):
                    items.append(
                        {
                            "item": title,
                            "severity": "high",
                            "trigger_keywords": [title],
                        }
                    )
                elif any(k in title for k in ("最佳实践", "建议", "原则", "方案")):
                    items.append(
                        {
                            "item": title,
                            "severity": "medium",
                            "trigger_keywords": [title],
                        }
                    )
        return items

    def _parse_checklist_item(self, raw: Dict) -> ChecklistItem:
        """解析 checklist 项"""
        return ChecklistItem(
            item=raw.get("item", ""),
            source=raw.get("source", ""),
            severity=raw.get("severity", "medium"),
            freshness_score=raw.get("freshness_score", 1.0),
            hit_count=raw.get("hit_count", 0),
            ignore_count=raw.get("ignore_count", 0),
            ignore_reasons=raw.get("ignore_reasons", []),
            last_hit=raw.get("last_hit"),
            last_ignore=raw.get("last_ignore"),
            applies_when=raw.get("applies_when", []),
            not_applies_when=raw.get("not_applies_when", []),
            trigger_keywords=raw.get("trigger_keywords", []),
            risk_patterns=raw.get("risk_patterns", []),
            detail=raw.get("detail", ""),
        )

    def _extract_scenario_tags(self, context_text: str) -> List[str]:
        """从上下文中提取场景标签"""
        text_lower = context_text.lower()
        tags = []
        for tag, keywords in self.SCENARIO_PATTERNS.items():
            for kw in keywords:
                if kw in text_lower:
                    tags.append(tag)
                    break
        return tags

    def _filter_by_scenario(
        self, items: List[ChecklistItem], scenario_tags: List[str]
    ) -> List[ChecklistItem]:
        """根据场景标签过滤 checklist"""
        if not scenario_tags:
            return items  # 没有场景信息，不过滤

        filtered = []
        for item in items:
            # 检查 not_applies_when：如果命中禁止场景，排除
            if item.not_applies_when:
                banned = set(item.not_applies_when)
                if banned & set(scenario_tags):
                    continue  # 当前场景在禁止列表中

            # 检查 applies_when：如果设定了适用场景，必须命中至少一个
            if item.applies_when:
                required = set(item.applies_when)
                if not (required & set(scenario_tags)):
                    continue  # 当前场景不匹配任何适用条件

            filtered.append(item)

        return filtered

    def _sort_by_relevance(
        self, items: List[ChecklistItem], scenario_tags: List[str] | None = None
    ) -> List[ChecklistItem]:
        """按相关性排序：场景匹配度 + 热力（hit/ignore 比率）+ 新鲜度

        六维评估矩阵应用：
        - 活跃度: freshness_score
        - 影响力: hit_count, severity
        - 场景: applies_when 匹配度
        - 负样本: ignore_count 降低权重
        """

        def score(item: ChecklistItem) -> float:
            # 1. 场景匹配度（最高优先级）
            scenario_match = 0.0
            if scenario_tags and item.applies_when:
                matched = set(item.applies_when) & set(scenario_tags)
                scenario_match = len(matched) / len(item.applies_when) * 0.4

            # 2. 热力比率（hit / (hit + ignore + 1)）
            total_interactions = item.hit_count + item.ignore_count + 1
            heat_ratio = item.hit_count / total_interactions
            heat_score = heat_ratio * 0.3

            # 3. 新鲜度基础分
            freshness = item.freshness_score * 0.2

            # 4. 严重性加成
            severity_weights = {"critical": 0.08, "high": 0.05, "medium": 0.02, "low": 0.0}
            severity_bonus = severity_weights.get(item.severity, 0.0)

            # 5. 最近命中加成
            recency_bonus = 0.0
            if item.last_hit:
                try:
                    last = datetime.fromisoformat(item.last_hit.replace("Z", "+00:00"))
                    days_ago = (datetime.now() - last).days
                    if days_ago < PRE_FLIGHT_INJECTOR_DURATION_BUCKET_WEEK_DAYS:
                        recency_bonus = 0.02
                    elif days_ago < PRE_FLIGHT_INJECTOR_DURATION_BUCKET_MONTH_DAYS:
                        recency_bonus = 0.01
                except (ValueError, TypeError):
                    logger.warning("[prophasis] (ValueError, TypeError) suppressed", exc_info=True)

            # 6. 用户画像偏好加成
            persona_bonus = 0.0
            if self.current_persona:
                persona_bonus = self._calc_persona_bonus(item)

            return (
                scenario_match
                + heat_score
                + freshness
                + severity_bonus
                + recency_bonus
                + persona_bonus
            )

        sorted_items = sorted(items, key=score, reverse=True)

        # ===== 回音室破解：探索-利用平衡 =====
        # 策略：在排序结果中注入约20%的"反画像"项
        # 目的是测试用户偏好是否仍然成立，防止画像固化
        sorted_items = self._apply_echo_chamber_breaker(sorted_items, scenario_tags)

        return sorted_items

    def _apply_echo_chamber_breaker(
        self, items: List[ChecklistItem], scenario_tags: List[str] | None = None
    ) -> List[ChecklistItem]:
        """
        回音室破解器：在排序结果中注入反画像项。

        原理：
        - 80% 利用（exploitation）：按画像偏好排序的高相关项
        - 20% 探索（exploration）：画像不太可能推荐的项
        - 如果被注入的项在后续周期中被用户采纳，说明画像需要更新
        - 如果被忽略，说明画像仍然准确

        这防止了"画像越来越窄，最后只推送用户已知喜欢的东西"。
        """
        if not self.current_persona or len(items) < 5:
            return items

        # 计算每个项的"反画像分数"（越低 = 越符合画像，越高 = 越反画像）
        def anti_persona_score(item: ChecklistItem) -> float:
            # 与 persona_bonus 相反：越不符合画像偏好，分数越高
            bonus = self._calc_persona_bonus(item)
            # 反画像分数 = 基础分 - 画像加成（加成高的反而分低）
            # 但我们想要的是：画像加成低的项
            return 1.0 - bonus  # 简单反转

        # 选出探索项（反画像分数最高的）
        explore_count = max(1, len(items) // 5)  # 20% 探索
        candidates = sorted(items, key=anti_persona_score, reverse=True)
        explore_items = candidates[:explore_count]

        # 混合策略：将探索项均匀插入到利用项中
        exploit_items = [i for i in items if i not in explore_items]

        result = []
        explore_idx = 0
        exploit_idx = 0
        total = len(items)

        for pos in range(total):
            # 每5个位置插入1个探索项
            if explore_idx < len(explore_items) and pos % 5 == 4:
                result.append(explore_items[explore_idx])
                explore_idx += 1
            elif exploit_idx < len(exploit_items):
                result.append(exploit_items[exploit_idx])
                exploit_idx += 1
            elif explore_idx < len(explore_items):
                result.append(explore_items[explore_idx])
                explore_idx += 1

        return result

    def _calc_persona_bonus(self, item: ChecklistItem) -> float:
        """根据用户画像偏好计算额外权重，跳过数据不足的维度"""
        bonus = 0.0
        if self.current_persona is None:
            return bonus
        value = self.current_persona.value
        # 数据不足的维度不参与计算
        ins = set(value.insufficient_dimensions or [])

        # 正确性>效率：增加severity高的项权重
        if "correctness_vs_efficiency" not in ins:
            if value.correctness_vs_efficiency > 0.6:
                if item.severity in ["critical", "high"]:
                    bonus += 0.05
            elif value.correctness_vs_efficiency < 0.4:
                # 效率优先：降低高severity的干扰
                if item.severity in ["low", "medium"]:
                    bonus += 0.03

        # 完美>完成：增加detail丰富的项权重
        if "perfection_vs_completion" not in ins:
            if value.perfection_vs_completion > 0.6:
                if len(item.detail) > 50:
                    bonus += 0.03

        # 深度>广度：增加有历史命中记录的项（说明用户深入关注过）
        if "depth_vs_breadth" not in ins:
            if value.depth_vs_breadth > 0.6:
                if item.hit_count > 2:
                    bonus += 0.03

        # 创新>稳妥：增加freshness高的项（新知识）
        if "innovation_vs_safety" not in ins:
            if value.innovation_vs_safety > 0.6:
                if item.freshness_score > 0.8:
                    bonus += 0.03

        return bonus

    def format_for_context(self, knowledge: LoadedKnowledge) -> str:
        """格式化为 context 注入文本（含装载存在感统计），根据用户画像调整输出风格"""
        if not knowledge or not knowledge.checklist:
            return ""

        detail_level = self._resolve_detail_level()
        lines = self._build_context_header(knowledge)
        display_items = self._filter_display_items(knowledge.checklist, detail_level)
        lines.extend(self._format_item_lines(display_items, detail_level))

        if knowledge.lessons_summary and detail_level != "minimal":
            lines.extend(["", "上次复盘要点：", knowledge.lessons_summary])

        lines.extend(self._build_context_footer(knowledge))
        return "\n".join(lines)

    def _resolve_detail_level(self) -> str:
        """根据当前用户画像决定详情级别。"""
        if not self.current_persona:
            return "balanced"
        pvc = self.current_persona.value.perfection_vs_completion
        if pvc > 0.6:
            return "thorough"
        if pvc < 0.4:
            return "minimal"
        return "balanced"

    @staticmethod
    def _filter_display_items(
        checklist: List[ChecklistItem], detail_level: str
    ) -> List[ChecklistItem]:
        """精简模式下只保留 critical/high 项。"""
        if detail_level != "minimal":
            return checklist
        filtered = [item for item in checklist if item.severity in ["critical", "high"]]
        return filtered if filtered else checklist[:3]

    @staticmethod
    def _format_item_lines(
        items: List[ChecklistItem], detail_level: str
    ) -> List[str]:
        """格式化 checklist 条目及其详情。"""
        severity_marks = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }
        lines = []
        for i, item in enumerate(items, 1):
            mark = severity_marks.get(item.severity, "⚪")
            usage_stat = ""
            if item.hit_count > 0 or item.ignore_count > 0:
                usage_stat = f" [H:{item.hit_count}/I:{item.ignore_count}]"
            lines.append(f"{i}. {mark} {item.item}{usage_stat}")

            if item.detail and detail_level != "minimal":
                if detail_level == "thorough":
                    lines.append(f"   详情: {item.detail}")
                else:
                    detail_short = (
                        item.detail[:100] + "..."
                        if len(item.detail) > 100
                        else item.detail
                    )
                    lines.append(f"   详情: {detail_short}")
        return lines

    @staticmethod
    def _build_context_header(knowledge: LoadedKnowledge) -> List[str]:
        """构建 context 注入文本头部。"""
        return [
            f"[Knowledge Loaded: {knowledge.task_type}/{knowledge.subtype} v{knowledge.version}]",
            "",
            "> 指令：以下 checklist 来自历史复盘经验，请你在接下来的回复中主动检查并遵循这些建议。",
            "> 如果某项不适用当前场景，可以忽略，但请在心中过一遍。",
            "",
            "本次任务的历史经验：",
        ]

    @staticmethod
    def _build_context_footer(knowledge: LoadedKnowledge) -> List[str]:
        """构建 context 注入文本尾部。"""
        lines = []
        if knowledge.total_items > 0:
            lines.append("")
            lines.append(
                f"[装载统计] 总计:{knowledge.total_items} 有命中:{knowledge.hit_items} "
                f"被忽略:{knowledge.ignored_items}"
            )
        if knowledge.is_compact:
            lines.append("(仅显示最关键的10条，完整清单见 wiki)")
        lines.extend(["", "注意：以上信息仅作为参考，请根据当前具体情况调整。"])
        return lines

    def mark_checklist_used(
        self, task_type: str, subtype: str, item_index: int, used: bool = True
    ) -> bool:
        """
        标记 checklist 项是否被使用（复盘时调用）

        Args:
            task_type: 任务类型
            subtype: 子类型
            item_index: checklist 项索引
            used: 是否被使用

        Returns:
            是否成功
        """
        latest = self._find_latest_version(task_type, subtype)
        if not latest:
            return False

        frontmatter, body = self._parse_retrospective(latest)
        if not frontmatter:
            return False

        checklist = frontmatter.get("checklist", [])
        if item_index >= len(checklist):
            return False

        # 更新命中信息
        item = checklist[item_index]
        if used:
            item["hit_count"] = item.get("hit_count", 0) + 1
            item["last_hit"] = datetime.now().isoformat()

        # 写回文件
        try:
            new_content = (
                f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n{body}"
            )
            latest.write_text(new_content, encoding="utf-8")
            return True
        except IOError:
            return False
