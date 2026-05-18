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

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple


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


@dataclass
class TaskTypeDefinition:
    """任务类型定义"""
    name: str                   # 类型名
    subtypes: Dict[str, List[str]]  # 子类型 -> 关键词列表
    keywords: List[str]         # 通用关键词
    expected_goal_prompts: List[str] = field(default_factory=list)  # 预期目标提示


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
                "code-review": ["代码审查", "code review", "cr", "审代码"],
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

    def __init__(self, history_path: Optional[str] = None):
        """
        Args:
            history_path: 历史任务记录路径，用于模式学习
        """
        self.history_path = Path(history_path).expanduser() if history_path else None
        self._history_cache = None

    # 冷启动计数器：记录每个任务类型被识别的次数（用于降低新类型的门槛）
    _cold_start_counts: Dict[str, int] = {}
    COLD_START_THRESHOLD = 3  # 前3次降低门槛
    COLD_START_BOOST = 0.2    # 冷启动加分

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

        if not combined_scores:
            return ClassificationResult(
                task_type="unknown",
                subtype="unknown",
                confidence=0.0,
                suggested_confirmation="ask"
            )

        # 取最高分
        best = max(combined_scores.items(), key=lambda x: x[1])
        task_type, score = best

        # 4. 冷启动策略：新任务类型前3次降低门槛
        count = self._cold_start_counts.get(task_type, 0)
        if count < self.COLD_START_THRESHOLD:
            score = min(score + self.COLD_START_BOOST, 1.0)
            self._cold_start_counts[task_type] = count + 1

        # 确定子类型
        subtype = self._determine_subtype(task_type, full_text_lower)

        # 确定确认策略
        confirmation = self._determine_confirmation(score)

        # 5. 预期目标提取
        expected_goals = self._extract_expected_goals(task_type, full_text_lower)

        return ClassificationResult(
            task_type=task_type,
            subtype=subtype,
            confidence=round(score, 3),
            matched_keywords=self._get_matched_keywords(task_type, full_text_lower),
            suggested_confirmation=confirmation,
            confirmed=(confirmation == "silent"),
            expected_goals=expected_goals,
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
        for task_type, definition in self.TASK_TAXONOMY.items():
            score = 0.0
            matched = []

            # 通用关键词匹配
            for kw in definition.keywords:
                if kw in text:
                    score += 1.0
                    matched.append(kw)

            # 子类型关键词匹配（权重更高）
            for subtype, sub_keywords in definition.subtypes.items():
                for kw in sub_keywords:
                    if kw in text:
                        score += 2.0  # 子类型关键词权重更高
                        matched.append(kw)

            # 归一化
            total_keywords = len(definition.keywords) + sum(len(v) for v in definition.subtypes.values())
            if total_keywords > 0:
                scores[task_type] = min(score / 3.0, 1.0)  # 封顶1.0

        return scores

    def _history_pattern_match(self, text: str) -> Dict[str, float]:
        """基于历史任务模式匹配"""
        history = self._load_history()
        if not history:
            return {}

        scores = {}
        for task_type, tasks in history.items():
            if len(tasks) < 2:
                continue

            # 计算历史任务中的高频词
            all_text = " ".join([t.get("summary", "") for t in tasks])
            history_words = set(all_text.lower().split())
            current_words = set(text.split())

            # Jaccard 相似度
            intersection = history_words & current_words
            union = history_words | current_words
            if union:
                similarity = len(intersection) / len(union)
                # 历史任务越多，权重越高
                weight = min(len(tasks) / 10.0, 1.0)
                scores[task_type] = similarity * weight

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

    def _determine_subtype(self, task_type: str, text: str) -> str:
        """确定子类型"""
        definition = self.TASK_TAXONOMY.get(task_type)
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
        definition = self.TASK_TAXONOMY.get(task_type)
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
        definition = self.TASK_TAXONOMY.get(task_type)
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
        definition = self.TASK_TAXONOMY.get(task_type)
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

def classify_task(messages: List[Dict], history_path: Optional[str] = None) -> ClassificationResult:
    """便捷函数：分类任务"""
    classifier = TaskClassifier(history_path=history_path)
    return classifier.classify(messages)
