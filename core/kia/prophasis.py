"""
Pre-flight Injector - 预加载注入器

从 wiki/retrospectives/ 装载历史经验，根据时间窗口决定策略：
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from .kairos import TimeWindow, TimeWindowType
from core.persona.delphi import PersonaStore, KnowledgeAligner
from core.config import get_config


import logging
logger = logging.getLogger(__name__)
@dataclass
class ChecklistItem:
    """校验清单项"""
    item: str
    source: str                      # 来源版本
    severity: str = "medium"         # critical/high/medium/low
    freshness_score: float = 1.0     # 新鲜度 0-1
    hit_count: int = 0               # 历史命中次数
    ignore_count: int = 0            # 被忽略次数（负样本学习）
    ignore_reasons: List[str] = field(default_factory=list)  # 忽略原因记录
    last_hit: Optional[str] = None   # 上次命中时间
    last_ignore: Optional[str] = None  # 上次忽略时间
    applies_when: List[str] = field(default_factory=list)
    not_applies_when: List[str] = field(default_factory=list)
    trigger_keywords: List[str] = field(default_factory=list)
    risk_patterns: List[str] = field(default_factory=list)
    detail: str = ""


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
    total_items: int = 0       # 总条目数（含未显示的）
    hit_items: int = 0         # 有命中记录的条目数
    ignored_items: int = 0     # 有被忽略记录的条目数


class PreFlightInjector:
    """预加载注入器"""

    WIKI_BASE = get_config().wiki_dir
    RETROSPECTIVES_DIR = WIKI_BASE / "06-Retrospectives"

    # 场景标签提取模式
    SCENARIO_PATTERNS = {
        "target:price_sensitive": ["价格敏感", "低价", "优惠", "便宜", "实惠"],
        "target:vip": ["vip", "高端", "高价值", "vip客户", "大客户"],
        "target:general": ["普通用户", "大众", "全体"],
        "scale:small": ["小规模", "小范围", "内部", "20人", "30人"],
        "scale:medium": ["中等规模", "50人", "100人"],
        "scale:large": ["大规模", "千人", "万人", "全网"],
    }

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.WIKI_BASE = Path(wiki_base).expanduser()
            self.RETROSPECTIVES_DIR = self.WIKI_BASE / "06-Retrospectives"
        self.persona_store = PersonaStore(self.WIKI_BASE)
        self.current_persona = None
        self._cache_db_path = Path.home() / ".mnemos" / "checklist_cache.db"
        self._warm_checklist_cache()

    def inject(self, task_type: str, subtype: str,
               time_window: TimeWindow,
               context_text: str = "") -> Optional[LoadedKnowledge]:
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
        # 0. 加载用户画像（如果可用）
        self._load_persona()

        # 1. 根据时间窗口决定策略
        if time_window.window in (TimeWindowType.IMMEDIATE, TimeWindowType.SHORT):
            return self._load_full(task_type, subtype, context_text)

        elif time_window.is_periodic:
            # 周期性任务：装载最新版本
            return self._load_full(task_type, subtype, context_text)

        else:
            # 中期/长期：不装载
            return None

    def _load_full(self, task_type: str, subtype: str,
                   context_text: str) -> Optional[LoadedKnowledge]:
        """装载完整清单（无专用复盘文件时从 Wiki 页面 fallback）"""
        latest = self._find_latest_version(task_type, subtype)
        if not latest:
            # Fallback：从缓存或 Wiki 页面搜索匹配类型的页面
            checklist_items = self._get_checklist_for_type(task_type)
            if checklist_items:
                return LoadedKnowledge(
                    task_type=task_type,
                    subtype=subtype,
                    version=1,
                    checklist=checklist_items,
                    lessons_summary="",
                    loaded_at=datetime.now().isoformat(),
                    is_compact=len(checklist_items) > 10,
                    total_items=len(checklist_items),
                    hit_items=0,
                    ignored_items=0,
                )
            return None

        frontmatter, body = self._parse_retrospective(latest)
        if not frontmatter:
            return None

        # 解析 checklist（优先 frontmatter，无则从正文生成）
        raw_checklist = frontmatter.get("checklist", [])
        if not raw_checklist:
            raw_checklist = self._generate_checklist_from_page(frontmatter, body)
        checklist_items = [self._parse_checklist_item(item) for item in raw_checklist]

        # 1. 场景适配过滤
        scenario_tags = self._extract_scenario_tags(context_text)
        checklist_items = self._filter_by_scenario(checklist_items, scenario_tags)

        # 2. 知识衰减排序（场景匹配度 + 热力 + 新鲜度）
        checklist_items = self._sort_by_relevance(checklist_items, scenario_tags)

        # 3. 限制数量（避免 context 超限）
        max_items = 10
        compact = len(checklist_items) > max_items
        displayed_items = checklist_items[:max_items]

        # 4. 静默装载存在感统计
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
        try:
            profile, _ = self.persona_store.load_persona()
            self.current_persona = profile
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at prophasis.py", exc_info=True)
            self.current_persona = None

    def _warm_checklist_cache(self):
        """遍历 04-Concepts/ 和 06-Retrospectives/，预热 checklist 缓存"""
        import sqlite3
        self._cache_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._cache_db_path))
        cursor = conn.cursor()
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
        conn.commit()

        allowed_types = {"retrospective", "problem-solution", "anti-pattern", "methodology", "insight"}
        dirs = [self.WIKI_BASE / "04-Concepts", self.WIKI_BASE / "06-Retrospectives"]
        current_paths = set()
        for d in dirs:
            if not d.exists():
                continue
            for md_file in d.rglob("*.md"):
                try:
                    page_path = str(md_file.relative_to(self.WIKI_BASE))
                    current_paths.add(page_path)
                    frontmatter, body = self._parse_retrospective(md_file)
                    page_type = frontmatter.get("类型", "")
                    if page_type not in allowed_types:
                        continue
                    title = frontmatter.get("title", frontmatter.get("name", md_file.stem))
                    task_types = []
                    applies_when = frontmatter.get("applies_when", {})
                    if isinstance(applies_when, dict):
                        task_types = applies_when.get("task_type", [])
                    if isinstance(task_types, str):
                        task_types = [task_types]
                    if not task_types:
                        task_types = frontmatter.get("task_type", [])
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
                    keywords = frontmatter.get("关键词", [])
                    triggers = frontmatter.get("触发器", [])
                    if isinstance(keywords, str):
                        keywords = [keywords]
                    if isinstance(triggers, str):
                        triggers = [triggers]
                    source = frontmatter.get("source", "wiki")
                    created_at = datetime.now().isoformat()
                    cursor.execute("DELETE FROM checklist_cache WHERE page_path = ?", (page_path,))
                    for tt in task_types:
                        for kw in keywords + triggers:
                            if not isinstance(kw, str):
                                continue
                            cursor.execute(
                                "INSERT INTO checklist_cache (task_type, keyword, title, page_path, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (tt, kw, title, page_path, source, created_at)
                            )
                except Exception:
                    continue
        cursor.execute("SELECT DISTINCT page_path FROM checklist_cache")
        existing_paths = {row[0] for row in cursor.fetchall()}
        for path in existing_paths - current_paths:
            cursor.execute("DELETE FROM checklist_cache WHERE page_path = ?", (path,))
        conn.commit()
        conn.close()

    def _get_checklist_for_type(self, task_type: str) -> List[ChecklistItem]:
        """优先从 SQLite 缓存获取 checklist，无命中则回退到文件搜索"""
        items = []
        try:
            import sqlite3
            conn = sqlite3.connect(str(self._cache_db_path))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT keyword, title, page_path, source FROM checklist_cache WHERE task_type = ? OR task_type = ''",
                (task_type,)
            )
            rows = cursor.fetchall()
            conn.close()
            for row in rows:
                keyword, title, page_path, source = row
                items.append(ChecklistItem(
                    item=keyword,
                    source=source or page_path or "wiki",
                    severity="medium",
                    trigger_keywords=[keyword],
                ))
        except Exception as e:
            logger.warning(f"读取 checklist 缓存失败: {e}")
            items = []
        if items:
            return items
        return self._get_checklist_from_files(task_type)

    def _get_checklist_from_files(self, task_type: str) -> List[ChecklistItem]:
        """回退：从 Wiki 文件搜索匹配的 checklist"""
        latest = self._find_wiki_fallback(task_type)
        if not latest:
            return []
        frontmatter, body = self._parse_retrospective(latest)
        raw_checklist = frontmatter.get("checklist", [])
        if not raw_checklist:
            raw_checklist = self._generate_checklist_from_page(frontmatter, body)
        return [self._parse_checklist_item(item) for item in raw_checklist]

    def _find_latest_version(self, task_type: str, subtype: str) -> Optional[Path]:
        """查找最新版本的复盘文件"""
        # 目录结构: wiki/retrospectives/{task_type}/{subtype}-v{N}.md
        # 或软链接: wiki/retrospectives/{task_type}/{subtype}-active.md
        task_dir = self.RETROSPECTIVES_DIR / task_type
        if not task_dir.exists():
            return None

        # 先检查 active 软链接
        active_link = task_dir / f"{subtype}-active.md"
        if active_link.exists() and active_link.is_symlink():
            resolved = active_link.resolve()
            if resolved.exists():
                return resolved

        # 否则找版本号最高的
        pattern = re.compile(re.escape(subtype) + r'-v(\d+)\.md$')
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
        frontmatter = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    body = parts[2].strip()
                except yaml.YAMLError:
                    pass

        return frontmatter, body

    def _find_wiki_fallback(self, task_type: str) -> Optional[Path]:
        """从 06-Retrospectives 搜索匹配 task_type 的页面（frontmatter + 文件名双重匹配）"""
        retro_dir = self.WIKI_BASE / "06-Retrospectives"
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
                elif task_type in ("coding", "debugging") and ("反模式" in stem or "问题-解决" in stem):
                    matched = True
                elif task_type == "design" and "决策记录" in stem:
                    matched = True
                # 3. 通用 fallback：文件名含 task_type 关键词
                elif task_type.lower() in stem.lower():
                    matched = True

                if matched:
                    candidates.append(md_file)
            except Exception:
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
                    items.append({
                        "item": f"相关知识: {kw}",
                        "severity": "medium",
                        "trigger_keywords": [kw],
                    })
        # 2. 从触发器生成
        triggers = frontmatter.get("触发器", [])
        if isinstance(triggers, list):
            for t in triggers[:3]:
                if isinstance(t, str):
                    items.append({
                        "item": f"触发场景: {t}",
                        "severity": "low",
                        "trigger_keywords": [t],
                    })
        # 3. 从正文标题中提取反模式/缺陷/注意事项
        import re
        # 匹配 Markdown 标题中的缺陷/反模式/注意/警告
        for line in body.split("\n"):
            m = re.match(r'^#{2,4}\s+(.+)$', line.strip())
            if m:
                title = m.group(1).strip()
                # 过滤掉通用标题，保留具体项
                if any(k in title for k in ("缺陷", "反模式", "注意", "警告", "风险", "坑", "问题")):
                    items.append({
                        "item": title,
                        "severity": "high",
                        "trigger_keywords": [title],
                    })
                elif any(k in title for k in ("最佳实践", "建议", "原则", "方案")):
                    items.append({
                        "item": title,
                        "severity": "medium",
                        "trigger_keywords": [title],
                    })
        return items

    def _parse_checklist_item(self, raw: Dict) -> ChecklistItem:
        """解析 checklist 项"""
        return ChecklistItem(
            item=raw.get("item", ""),
            source=raw.get("source", ""),
            severity=raw.get("severity", "medium"),
            freshness_score=raw.get("freshness_score", 1.0),
            hit_count=raw.get("hit_count", 0),
            last_hit=raw.get("last_hit"),
            applies_when=raw.get("applies_when", []),
            not_applies_when=raw.get("not_applies_when", []),
            trigger_keywords=raw.get("trigger_keywords", []),
            risk_patterns=raw.get("risk_patterns", []),
            detail=raw.get("detail", "")
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

    def _filter_by_scenario(self, items: List[ChecklistItem],
                            scenario_tags: List[str]) -> List[ChecklistItem]:
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

    def _sort_by_relevance(self, items: List[ChecklistItem],
                           scenario_tags: List[str] = None) -> List[ChecklistItem]:
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
                    last = datetime.fromisoformat(item.last_hit.replace('Z', '+00:00'))
                    days_ago = (datetime.now() - last).days
                    if days_ago < 7:
                        recency_bonus = 0.02
                    elif days_ago < 30:
                        recency_bonus = 0.01
                except (ValueError, TypeError):
                    pass

            # 6. 用户画像偏好加成
            persona_bonus = 0.0
            if self.current_persona:
                persona_bonus = self._calc_persona_bonus(item)

            return scenario_match + heat_score + freshness + severity_bonus + recency_bonus + persona_bonus

        sorted_items = sorted(items, key=score, reverse=True)

        # ===== 回音室破解：探索-利用平衡 =====
        # 策略：在排序结果中注入约20%的"反画像"项
        # 目的是测试用户偏好是否仍然成立，防止画像固化
        sorted_items = self._apply_echo_chamber_breaker(sorted_items, scenario_tags)

        return sorted_items

    def _apply_echo_chamber_breaker(self, items: List[ChecklistItem],
                                     scenario_tags: List[str] = None) -> List[ChecklistItem]:
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

        # 根据画像调整detail_level
        detail_level = "balanced"
        if self.current_persona:
            # perfection_vs_completion > 0.6 = 详尽, < 0.4 = 精简
            if self.current_persona.value.perfection_vs_completion > 0.6:
                detail_level = "thorough"
            elif self.current_persona.value.perfection_vs_completion < 0.4:
                detail_level = "minimal"

        lines = [
            f"[Knowledge Loaded: {knowledge.task_type}/{knowledge.subtype} v{knowledge.version}]",
            "",
            "> 指令：以下 checklist 来自历史复盘经验，请你在接下来的回复中主动检查并遵循这些建议。",
            "> 如果某项不适用当前场景，可以忽略，但请在心中过一遍。",
            "",
            "本次任务的历史经验：",
        ]

        # 精简模式下只保留critical/high项
        display_items = knowledge.checklist
        if detail_level == "minimal":
            display_items = [item for item in knowledge.checklist if item.severity in ["critical", "high"]]
            if not display_items:
                display_items = knowledge.checklist[:3]  # 至少保留3条

        for i, item in enumerate(display_items, 1):
            severity_mark = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(item.severity, "⚪")
            # 显示命中/忽略统计
            usage_stat = ""
            if item.hit_count > 0 or item.ignore_count > 0:
                usage_stat = f" [H:{item.hit_count}/I:{item.ignore_count}]"
            lines.append(f"{i}. {severity_mark} {item.item}{usage_stat}")

            # 根据detail_level控制详情输出
            if item.detail:
                if detail_level == "thorough":
                    lines.append(f"   详情: {item.detail}")
                elif detail_level == "balanced":
                    # 只显示前100字
                    detail_short = item.detail[:100] + "..." if len(item.detail) > 100 else item.detail
                    lines.append(f"   详情: {detail_short}")
                # minimal模式下不显示detail

        if knowledge.lessons_summary and detail_level != "minimal":
            lines.extend([
                "",
                "上次复盘要点：",
                knowledge.lessons_summary,
            ])

        # 装载存在感统计
        if knowledge.total_items > 0:
            lines.append("")
            lines.append(
                f"[装载统计] 总计:{knowledge.total_items} 有命中:{knowledge.hit_items} "
                f"被忽略:{knowledge.ignored_items}"
            )

        if knowledge.is_compact:
            lines.append("(仅显示最关键的10条，完整清单见 wiki)")

        lines.extend([
            "",
            "注意：以上信息仅作为参考，请根据当前具体情况调整。",
        ])

        return "\n".join(lines)

    def mark_checklist_used(self, task_type: str, subtype: str,
                            item_index: int, used: bool = True) -> bool:
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
            new_content = f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n{body}"
            latest.write_text(new_content, encoding="utf-8")
            return True
        except IOError:
            return False

    def list_available_types(self) -> List[Tuple[str, str, int]]:
        """
        列出所有可用的复盘类型

        Returns:
            [(task_type, subtype, version), ...]
        """
        result = []
        if not self.RETROSPECTIVES_DIR.exists():
            return result

        for task_dir in self.RETROSPECTIVES_DIR.iterdir():
            if not task_dir.is_dir():
                continue
            task_type = task_dir.name

            for f in task_dir.glob("*-v*.md"):
                match = re.search(r'(.+)-v(\d+)\.md$', f.name)
                if match:
                    subtype = match.group(1)
                    version = int(match.group(2))
                    result.append((task_type, subtype, version))

        return result


# ========== 便捷函数 ==========

def load_knowledge(task_type: str, subtype: str,
                   time_window: TimeWindow,
                   context_text: str = "") -> Optional[LoadedKnowledge]:
    """便捷函数：加载知识"""
    injector = PreFlightInjector()
    return injector.inject(task_type, subtype, time_window, context_text)
