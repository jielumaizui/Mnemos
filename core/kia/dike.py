"""
Task Classifier - 通用任务分类器

支持多维度判定：
1. 关键词快速匹配
2. 历史任务模式学习
3. LLM语义确认（可选）

确认策略：
- 置信度 > 0.9：静默确认
- 置信度 0.7-0.9：静默提示
- 置信度 < 0.7：主动询问
"""
# Dike — 正义女神 — 任务分类器，裁决与归类
# 原模块: task_classifier.py



import json
import re
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import logging
logger = logging.getLogger(__name__)
try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml 是项目依赖，保留降级兜底
    yaml = None


@dataclass
class ClassificationResult:
    """分类结果"""
    task_type: str              # 主类型，如 "coding"
    subtype: str                # 子类型，如 "python"
    confidence: float           # 置信度 0-1
    matched_keywords: List[str] = field(default_factory=list)
    suggested_confirmation: str = ""  # "silent" / "hint" / "ask"
    confirmed: bool = False     # 是否已确认
    expected_goals: Dict = field(default_factory=dict)  # 预期目标
    context_summary: str = ""   # 上下文摘要
    top_types: List[Tuple[str, float]] = field(default_factory=list)
    all_scores: Dict[str, float] = field(default_factory=dict)
    mixed_intent: bool = False
    matched_keywords_by_type: Dict[str, List[str]] = field(default_factory=dict)
    primary_type: str = ""
    primary_subtype: str = ""
    primary_confidence: float = 0.0

    def __post_init__(self):
        # 兼容蓝图新字段与旧调用方字段。
        if not self.primary_type:
            self.primary_type = self.task_type
        if not self.primary_subtype:
            self.primary_subtype = self.subtype
        if not self.primary_confidence:
            self.primary_confidence = self.confidence
        if not self.top_types and self.task_type != "unknown":
            self.top_types = [(self.task_type, self.confidence)]
        if not self.all_scores and self.task_type != "unknown":
            self.all_scores = {self.task_type: self.confidence}


@dataclass
class TaskTypeDefinition:
    """任务类型定义"""
    name: str                   # 类型名
    subtypes: Dict[str, List[str]]  # 子类型 -> 关键词列表
    keywords: List[str]         # 通用关键词
    expected_goal_prompts: List[str] = field(default_factory=list)  # 预期目标提示


class TaskLearner:
    """用户反馈学习器：记录纠正反馈并调整关键词权重。"""

    def __init__(
        self,
        feedback_db: Optional[str] = None,
        taxonomy: Optional[Dict[str, TaskTypeDefinition]] = None,
    ):
        if feedback_db:
            self.feedback_db = Path(feedback_db).expanduser()
        else:
            from core.config import get_config
            self.feedback_db = get_config().data_dir / "task_classifier.db"
        self.taxonomy = taxonomy or TaskClassifier.TASK_TAXONOMY
        self._init_db()

    def _init_db(self):
        self.feedback_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.feedback_db), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS classification_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_hash TEXT NOT NULL,
                    predicted TEXT NOT NULL,
                    actual TEXT NOT NULL,
                    text_preview TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS keyword_weights (
                    task_type TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (task_type, keyword)
                )
            """)

    def record_feedback(self, text: str, predicted: str, actual: str):
        text_lower = text.lower()
        text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        with sqlite3.connect(str(self.feedback_db), timeout=10) as conn:
            conn.execute("""
                INSERT INTO classification_feedback
                (text_hash, predicted, actual, text_preview, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (text_hash, predicted, actual, text[:200], datetime.now().isoformat()))

            for keyword in self._extract_matched_keywords(predicted, text_lower):
                self._update_keyword_weight(conn, predicted, keyword, 0.8)
            for keyword in self._extract_matched_keywords(actual, text_lower):
                self._update_keyword_weight(conn, actual, keyword, 1.2)

    def get_adjusted_weight(self, task_type: str, keyword: str) -> float:
        with sqlite3.connect(str(self.feedback_db), timeout=10) as conn:
            row = conn.execute("""
                SELECT weight FROM keyword_weights
                WHERE task_type = ? AND keyword = ?
            """, (task_type, keyword)).fetchone()
        return float(row[0]) if row else 1.0

    def _extract_matched_keywords(self, task_type: str, text: str) -> List[str]:
        definition = self.taxonomy.get(task_type)
        if not definition:
            return []
        keywords = list(definition.keywords)
        for subtype_keywords in definition.subtypes.values():
            keywords.extend(subtype_keywords)
        return [kw for kw in keywords if kw in text]

    @staticmethod
    def _update_keyword_weight(conn: sqlite3.Connection, task_type: str, keyword: str, value: float):
        conn.execute("""
            INSERT INTO keyword_weights (task_type, keyword, weight, sample_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(task_type, keyword) DO UPDATE SET
                weight = (weight * sample_count + excluded.weight) / (sample_count + 1),
                sample_count = sample_count + 1
        """, (task_type, keyword, value))


class TaskClassifier:
    """通用任务分类器"""

    # 预定义任务类型图谱
    TASK_TAXONOMY = {
        "coding": TaskTypeDefinition(
            name="coding",
            subtypes={
                "python": ["python", "py", "python脚本", "python代码", "python程序"],
                "javascript": ["js", "javascript", "前端", "react", "vue", "node"],
                "sql": ["sql", "数据库", "查询", "表结构", "索引"],
                "shell": ["shell", "bash", "脚本", "命令行", "自动化脚本"],
                "data-pipeline": ["pipeline", "etl", "数据流", "数据管道", "批处理"],
            },
            keywords=["写代码", "程序", "bug", "debug", "函数", "类", "接口", "api",
                     "代码", "编程", "开发", "实现", "重构", "优化代码", "报错"],
            expected_goal_prompts=[
                "预期功能是什么？",
                "输入输出格式？",
                "性能要求？",
            ],
        ),
        "marketing": TaskTypeDefinition(
            name="marketing",
            subtypes={
                "event-planning": ["活动", "策划", "活动策划", "线下活动", "线上活动"],
                "content-strategy": ["内容", "文案", "公众号", "文章", "内容策略"],
                "growth-hack": ["拉新", "增长", "获客", "裂变", "引流", "转化"],
            },
            keywords=["宣发", "推广", "营销", "品牌", "活动", "策划", "运营",
                     "客户", "用户", "留存", "活跃", "roi", "投放"],
            expected_goal_prompts=[
                "预期参与人数？",
                "目标转化率？",
                "预算范围？",
                "目标客户群体？",
            ],
        ),
        "analysis": TaskTypeDefinition(
            name="analysis",
            subtypes={
                "user-behavior": ["用户行为", "用户画像", "用户路径", "漏斗分析"],
                "data-analysis": ["数据分析", "报表", "统计", "可视化", "图表"],
                "ab-test": ["ab测试", "实验", "对照组", "显著性", "假设检验"],
            },
            keywords=["分析", "数据", "报表", "统计", "漏斗", "转化率", "指标",
                     "趋势", "对比", "归因", "洞察", "维度", "维度分析"],
            expected_goal_prompts=[
                "分析目标是什么？",
                "关键指标有哪些？",
                "时间范围？",
            ],
        ),
        "strategy": TaskTypeDefinition(
            name="strategy",
            subtypes={
                "product-launch": ["产品发布", "上线", "发布策略", "go-to-market"],
                "market-entry": ["市场进入", "新市场", "拓展", "渠道"],
                "competitive-analysis": ["竞品分析", "竞争", "对手", "市场格局"],
            },
            keywords=["战略", "布局", "规划", "路线图", "竞品", "定位", "愿景",
                     "目标", "方向", "策略", "打法", "商业模式", "护城河"],
            expected_goal_prompts=[
                "目标市场/客群？",
                "预期达成什么效果？",
                "时间周期？",
                "关键里程碑？",
            ],
        ),
        "writing": TaskTypeDefinition(
            name="writing",
            subtypes={
                "documentation": ["文档", "说明", "手册", "wiki", "知识库"],
                "proposal": ["方案", "提案", "计划书", "汇报", "ppt"],
                "blog": ["博客", "文章", "公众号", "知乎", "自媒体"],
            },
            keywords=["写", "文档", "方案", "报告", "文章", "内容", "编辑",
                     "整理", "总结", "撰写", "稿子", "文案"],
            expected_goal_prompts=[
                "目标读者是谁？",
                "预期篇幅/深度？",
                "核心信息是什么？",
            ],
        ),
        "review": TaskTypeDefinition(
            name="review",
            subtypes={
                "code-review": ["代码审查", "code review", "cr", "审代码", "代码"],
                "design-review": ["设计评审", "方案评审", "架构评审"],
            },
            keywords=["审查", "评审", "review", "检查", "评估", "验收",
                     "把关", "确认", "审核"],
            expected_goal_prompts=[
                "审查标准是什么？",
                "重点关注哪些方面？",
            ],
        ),
    }

    # 子类型到主类型的映射
    SUBTYPE_TO_PARENT = {}
    for _parent, _def in TASK_TAXONOMY.items():
        for _subtype in _def.subtypes:
            SUBTYPE_TO_PARENT[_subtype] = _parent

    def __init__(
        self,
        config_path: Optional[str] = None,
        history_db: Optional[str] = None,
        history_path: Optional[str] = None,
        feedback_db: Optional[str] = None,
    ):
        """
        Args:
            config_path: YAML 任务类型配置路径
            history_db: SQLite 历史任务库路径
            history_path: 历史任务记录路径，用于模式学习
            feedback_db: 用户反馈权重库路径
        """
        self.task_taxonomy = self._load_config(config_path)
        self.history_path = Path(history_path).expanduser() if history_path else None
        if history_db:
            self.history_db = Path(history_db).expanduser()
        else:
            from core.config import get_config
            self.history_db = get_config().data_dir / "task_classifier.db"
        self._history_cache = None
        self.learner = TaskLearner(feedback_db or str(self.history_db), self.task_taxonomy)
        self._init_history_db()

    # 冷启动计数器：记录每个任务类型被识别的次数（用于降低新类型的门槛）
    _cold_start_counts: Dict[str, int] = {}
    COLD_START_THRESHOLD = 3  # 前3次降低门槛
    COLD_START_BOOST = 0.2    # 冷启动加分

    def _load_config(self, config_path: Optional[str]) -> Dict[str, TaskTypeDefinition]:
        """加载任务类型配置；无配置或解析失败时回退内置分类表。"""
        if not config_path:
            return self.TASK_TAXONOMY

        path = Path(config_path).expanduser()
        if not path.exists() or yaml is None:
            return self.TASK_TAXONOMY

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at dike.py", exc_info=True)
            return self.TASK_TAXONOMY

        raw_task_types = data.get("task_types", {})
        if not isinstance(raw_task_types, dict):
            return self.TASK_TAXONOMY

        taxonomy: Dict[str, TaskTypeDefinition] = {}
        for task_type, info in raw_task_types.items():
            if not isinstance(info, dict):
                continue
            taxonomy[task_type] = TaskTypeDefinition(
                name=info.get("name", task_type),
                keywords=list(info.get("keywords", [])),
                subtypes={
                    str(subtype): list(keywords or [])
                    for subtype, keywords in (info.get("subtypes", {}) or {}).items()
                },
                expected_goal_prompts=list(
                    info.get("expected_goals")
                    or info.get("expected_goal_prompts")
                    or []
                ),
            )

        return taxonomy or self.TASK_TAXONOMY

    def _init_history_db(self):
        """初始化 SQLite 历史任务表。"""
        self.history_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.history_db), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_classification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    task_type TEXT NOT NULL,
                    subtype TEXT,
                    summary TEXT,
                    keywords TEXT,
                    confidence REAL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_history_type
                ON task_classification_history(task_type, created_at)
            """)

    def record_history(
        self,
        session_id: str,
        result: ClassificationResult,
        summary: str,
    ):
        """记录分类历史，供后续 Jaccard 模式匹配使用。"""
        with sqlite3.connect(str(self.history_db), timeout=10) as conn:
            conn.execute("""
                INSERT INTO task_classification_history
                (session_id, task_type, subtype, summary, keywords, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id,
                result.task_type,
                result.subtype,
                summary,
                json.dumps(result.matched_keywords, ensure_ascii=False),
                result.confidence,
                datetime.now().isoformat(),
            ))

    def classify(self, messages: List[Dict]) -> ClassificationResult:
        """
        分类任务类型（含冷启动策略）

        Args:
            messages: 会话消息列表，每项包含 'role' 和 'content'

        Returns:
            ClassificationResult
        """
        # 合并所有消息内容
        full_text = " ".join([m.get("content", "") for m in messages if m.get("content")])
        full_text_lower = full_text.lower()

        # 1. 关键词匹配
        keyword_scores = self._keyword_match(full_text_lower)

        # 2. 历史模式匹配
        history_scores = self._history_pattern_match(full_text_lower)

        # 3. 合并得分
        combined_scores = self._combine_scores(keyword_scores, history_scores)
        combined_scores = {k: round(v, 3) for k, v in combined_scores.items() if v > 0}

        if not combined_scores:
            return ClassificationResult(
                task_type="unknown",
                subtype="unknown",
                confidence=0.0,
                suggested_confirmation="ask",
                top_types=[],
                all_scores={},
                primary_type="unknown",
                primary_subtype="unknown",
            )

        sorted_scores = self._sort_scores(combined_scores)
        task_type, score = sorted_scores[0]

        # 4. 冷启动策略：新任务类型前3次降低门槛
        count = self._cold_start_counts.get(task_type, 0)
        if count < self.COLD_START_THRESHOLD:
            score = min(score + self.COLD_START_BOOST, 1.0)
            self._cold_start_counts[task_type] = count + 1
            combined_scores[task_type] = round(score, 3)
            sorted_scores = self._sort_scores(combined_scores)

        # 确定子类型
        subtype = self._determine_subtype(task_type, full_text_lower)

        # 确定确认策略
        confirmation = self._determine_confirmation(score)

        # 5. 预期目标提取
        expected_goals = self._extract_expected_goals(task_type, full_text_lower)
        secondary_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0
        mixed_intent = bool(score > 0 and secondary_score > 0.4 and secondary_score / score > 0.6)
        matched_by_type = {
            t: self._get_matched_keywords(t, full_text_lower)
            for t, s in sorted_scores[:3]
            if s > 0
        }

        return ClassificationResult(
            task_type=task_type,
            subtype=subtype,
            confidence=round(score, 3),
            matched_keywords=self._get_matched_keywords(task_type, full_text_lower),
            suggested_confirmation=confirmation,
            confirmed=(confirmation == "silent"),
            expected_goals=expected_goals,
            top_types=sorted_scores[:3],
            all_scores=dict(sorted_scores),
            mixed_intent=mixed_intent,
            matched_keywords_by_type=matched_by_type,
            primary_type=task_type,
            primary_subtype=subtype,
            primary_confidence=round(score, 3),
        )

    def classify_and_confirm(self, messages: List[Dict],
                             llm_confirm_callback=None) -> ClassificationResult:
        """
        分类 + 确认（完整流程）

        Args:
            messages: 会话消息
            llm_confirm_callback: 可选的LLM确认回调函数
                                  签名: fn(task_type, subtype, context) -> (confirmed: bool, confidence: float)

        Returns:
            ClassificationResult（confirmed 字段已更新）
        """
        result = self.classify(messages)

        # 如果LLM确认回调存在且置信度不够高，调用LLM
        if llm_confirm_callback and result.confidence < 0.9:
            confirmed, llm_confidence = llm_confirm_callback(
                result.task_type,
                result.subtype,
                messages
            )
            # 取平均
            result.confidence = round((result.confidence + llm_confidence) / 2, 3)
            result.confirmed = confirmed
            result.suggested_confirmation = self._determine_confirmation(result.confidence)

        return result

    def _keyword_match(self, text: str) -> Dict[str, float]:
        """关键词匹配，返回各类型得分"""
        scores = {}
        for task_type, definition in self.task_taxonomy.items():
            score = 0.0
            matched = []

            # 通用关键词匹配
            for kw in definition.keywords:
                if kw in text:
                    score += 1.0 * self.learner.get_adjusted_weight(task_type, kw)
                    matched.append(kw)

            # 子类型关键词匹配（权重更高）
            for subtype, sub_keywords in definition.subtypes.items():
                for kw in sub_keywords:
                    if kw in text:
                        score += 2.0 * self.learner.get_adjusted_weight(task_type, kw)
                        matched.append(kw)

            # 归一化
            total_keywords = len(definition.keywords) + sum(len(v) for v in definition.subtypes.values())
            if total_keywords > 0:
                scores[task_type] = min(score / 3.0, 1.0)  # 封顶1.0

        return scores

    def _history_pattern_match(self, text: str) -> Dict[str, float]:
        """基于历史任务模式匹配"""
        scores = {}
        with sqlite3.connect(str(self.history_db), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            for task_type in self.task_taxonomy.keys():
                rows = conn.execute("""
                    SELECT summary FROM task_classification_history
                    WHERE task_type = ?
                    ORDER BY created_at DESC
                    LIMIT 20
                """, (task_type,)).fetchall()
                if len(rows) < 2:
                    continue

                all_text = " ".join(row["summary"] or "" for row in rows)
                history_words = set(all_text.lower().split())
                current_words = set(text.split())

                intersection = history_words & current_words
                union = history_words | current_words
                if union:
                    similarity = len(intersection) / len(union)
                    weight = min(len(rows) / 10.0, 1.0)
                    scores[task_type] = similarity * weight

        # 兼容旧 JSON history_path；SQLite 为主，旧数据只补充未命中的类型。
        for task_type, tasks in self._load_history().items():
            if task_type in scores or len(tasks) < 2:
                continue
            all_text = " ".join(t.get("summary", "") for t in tasks)
            history_words = set(all_text.lower().split())
            current_words = set(text.split())
            union = history_words | current_words
            if union:
                scores[task_type] = (len(history_words & current_words) / len(union)) * min(len(tasks) / 10.0, 1.0)

        return scores

    def _combine_scores(self, keyword_scores: Dict, history_scores: Dict) -> Dict[str, float]:
        """合并关键词得分和历史得分"""
        all_types = set(keyword_scores.keys()) | set(history_scores.keys())
        combined = {}

        for task_type in all_types:
            kw_score = keyword_scores.get(task_type, 0.0)
            hist_score = history_scores.get(task_type, 0.0)
            # 关键词为主（权重0.7），历史为辅（权重0.3）
            combined[task_type] = kw_score * 0.7 + hist_score * 0.3

        return combined

    def _sort_scores(self, scores: Dict[str, float]) -> List[Tuple[str, float]]:
        """按分数降序排序；同分时按分类表顺序，保证结果稳定。"""
        order = {task_type: i for i, task_type in enumerate(self.task_taxonomy.keys())}
        return sorted(scores.items(), key=lambda x: (-x[1], order.get(x[0], 999)))

    def _determine_subtype(self, task_type: str, text: str) -> str:
        """确定子类型"""
        definition = self.task_taxonomy.get(task_type)
        if not definition:
            return "unknown"

        best_subtype = "general"
        best_score = 0

        for subtype, keywords in definition.subtypes.items():
            score = sum(2.0 for kw in keywords if kw in text)
            if score > best_score:
                best_score = score
                best_subtype = subtype

        return best_subtype

    def _extract_expected_goals(self, task_type: str, text: str) -> Dict[str, str]:
        """从消息文本中提取预期目标（参与人数、转化率、时间等）"""
        goals = {}

        # 参与人数/规模
        participant_match = re.search(r'(\d+)\s*人', text)
        if participant_match:
            goals["participants"] = participant_match.group(1)

        # 转化率
        conversion_match = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:转化率|转化)', text)
        if conversion_match:
            goals["conversion_rate"] = conversion_match.group(1) + "%"

        # 预算
        budget_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:万|k|千)?\s*(?:元|预算|费用)', text)
        if budget_match:
            goals["budget"] = budget_match.group(1)

        # 时间/周期
        time_match = re.search(r'(\d+)\s*(?:天|周|月|年)', text)
        if time_match:
            goals["duration"] = time_match.group(0)

        # 目标/预期效果
        if "目标" in text or "预期" in text:
            goal_match = re.search(r'[目标预期].*?[:：]\s*(.+?)(?:[，。；]|$)', text)
            if goal_match:
                goals["target"] = goal_match.group(1).strip()

        # 根据任务类型补充默认提示
        definition = self.task_taxonomy.get(task_type)
        if definition and not goals:
            # 如果没有提取到目标，使用默认提示
            prompts = definition.expected_goal_prompts
            if prompts:
                goals["_prompts"] = prompts

        return goals

    def _determine_confirmation(self, confidence: float) -> str:
        """根据置信度确定确认策略"""
        if confidence >= 0.9:
            return "silent"      # 静默确认
        elif confidence >= 0.7:
            return "hint"        # 静默提示
        else:
            return "ask"         # 主动询问

    def _get_matched_keywords(self, task_type: str, text: str) -> List[str]:
        """获取匹配到的关键词"""
        definition = self.task_taxonomy.get(task_type)
        if not definition:
            return []

        matched = []
        for kw in definition.keywords:
            if kw in text and kw not in matched:
                matched.append(kw)
        for subtype_keywords in definition.subtypes.values():
            for kw in subtype_keywords:
                if kw in text and kw not in matched:
                    matched.append(kw)

        return matched[:10]  # 最多返回10个

    def _load_history(self) -> Dict[str, List[Dict]]:
        """加载历史任务记录"""
        if self._history_cache is not None:
            return self._history_cache

        if not self.history_path or not self.history_path.exists():
            return {}

        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            self._history_cache = data
            return data
        except (json.JSONDecodeError, IOError):
            return {}

    def get_expected_goal_prompts(self, task_type: str) -> List[str]:
        """获取预期目标提示问题"""
        definition = self.task_taxonomy.get(task_type)
        if definition:
            return definition.expected_goal_prompts
        return ["预期目标是什么？", "期望达成什么效果？"]

    def get_task_type_label(self, task_type: str, subtype: str = "") -> str:
        """获取任务类型的中文标签"""
        labels = {
            "coding": "编程开发",
            "marketing": "营销策划",
            "analysis": "数据分析",
            "strategy": "战略规划",
            "writing": "内容撰写",
            "review": "审查评审",
        }
        sublabels = {
            "python": "Python",
            "javascript": "JavaScript",
            "sql": "SQL",
            "shell": "Shell",
            "data-pipeline": "数据管道",
            "event-planning": "活动策划",
            "content-strategy": "内容策略",
            "growth-hack": "增长黑客",
            "user-behavior": "用户行为",
            "data-analysis": "数据分析",
            "ab-test": "AB测试",
            "product-launch": "产品发布",
            "market-entry": "市场进入",
            "competitive-analysis": "竞品分析",
            "documentation": "文档撰写",
            "proposal": "方案撰写",
            "blog": "博客文章",
            "code-review": "代码审查",
            "design-review": "设计评审",
        }

        base = labels.get(task_type, task_type)
        sub = sublabels.get(subtype, subtype)
        if sub and sub != "general":
            return f"{base}/{sub}"
        return base


# ========== 便捷函数 ==========

def classify_task(
    messages: List[Dict],
    history_path: Optional[str] = None,
    config_path: Optional[str] = None,
    history_db: Optional[str] = None,
    feedback_db: Optional[str] = None,
) -> ClassificationResult:
    """便捷函数：分类任务"""
    classifier = TaskClassifier(
        config_path=config_path,
        history_db=history_db,
        history_path=history_path,
        feedback_db=feedback_db,
    )
    return classifier.classify(messages)
