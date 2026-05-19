"""
Predictive Push - 主动预测式知识推送

基于用户当前上下文，主动推荐相关知识：
- 不是等用户搜索，而是预判用户可能需要什么
- 推送时机：用户提问、表达困惑、开始新主题
- 匹配维度：关键词、场景标签、工具实体、情感倾向
- 输出格式：简洁的上下文围栏 <wiki-context>

设计原则：
- 不打扰：距离上次推送 < 10 分钟不重复推送
- 精准：匹配分数 > 0.6 才推送
- 简洁：最多推送 3 条知识，每条 2-3 句话
- 可选：用户可以说"忽略"或"记住了"

推送触发规则：
1. 用户明确提问（"怎么..." / "为什么..."）→ 高优先级
2. 用户表达困惑（"卡住了" / "报错了" / "不懂"）→ 中优先级
3. 用户开始新主题（上下文切换）→ 低优先级
4. 用户提及特定工具/框架 → 定向推送
"""

from __future__ import annotations

import re
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.config import get_config


@dataclass
class ContextSignal:
    """上下文信号"""
    signal_type: str           # question / confused / new_topic / tool_mention / emotional
    keywords: List[str] = field(default_factory=list)
    mentioned_tools: List[str] = field(default_factory=list)
    emotional_state: str = ""  # frustrated / curious / urgent / neutral
    confidence: float = 0.5


@dataclass
class KnowledgeMatch:
    """知识匹配结果"""
    page_path: str
    page_title: str = ""
    match_score: float = 0.0
    match_reason: str = ""
    relevant_excerpt: str = ""  # 最相关的段落摘录
    push_priority: str = "low"  # high / medium / low


@dataclass
class PushDecision:
    """推送决策"""
    should_push: bool
    reason: str
    matches: List[KnowledgeMatch] = field(default_factory=list)
    push_content: str = ""
    cooldown_seconds: int = 600  # 默认冷却 10 分钟


class PredictivePushEngine:
    """预测推送引擎"""

    # 匹配权重
    MATCH_WEIGHTS = {
        "tool_match": 0.35,       # 工具实体匹配权重最高
        "scenario_match": 0.25,   # 场景标签匹配
        "keyword_match": 0.20,    # 核心概念匹配
        "title_match": 0.15,      # 标题关键词匹配
        "form_bonus": 0.05,       # 形态 bonus（问题-解决类在提问时加分）
    }

    # 推送阈值
    PUSH_THRESHOLD = 0.60
    MAX_PUSH_COUNT = 3
    COOLDOWN_SECONDS = 600  # 10 分钟冷却

    # 困惑信号词
    CONFUSED_SIGNALS = [
        "卡住", "报错", "错误", "失败", "不行", "不对", "不懂", "不会",
        "困惑", "疑问", "不确定", "没搞懂", "怎么回事", "为什么不行",
        "stuck", "error", "fail", "wrong", "confused", "don't understand",
        "not working", "doesn't work",
    ]

    # 提问信号词
    QUESTION_SIGNALS = [
        "怎么", "如何", "为什么", "是什么", "有哪些", "哪个", "可否",
        "能否", "请问", "help", "how to", "how do", "what is", "why",
        "which", "can you", "could you",
    ]

    # 紧急信号词
    URGENT_SIGNALS = [
        " urgently", "紧急", "急", "马上", "立刻", "现在就要", "deadline",
        "production", "线上", "生产环境", "挂了", "崩了",
    ]

    def __init__(self, wiki_base: str = None, db_path: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.db_path = Path(db_path) if db_path else (
            get_config().data_dir / "push.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # 缓存页面索引（避免每次扫描文件系统）
        self._page_index: Optional[List[Dict]] = None
        self._index_timestamp: Optional[datetime] = None

    def _init_db(self):
        """初始化数据库"""
        schema = """
        CREATE TABLE IF NOT EXISTS push_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            trigger_context TEXT,
            pushed_pages TEXT,        -- JSON
            user_response TEXT,       -- accept / ignore / dismiss / "记住了"
            session_id TEXT
        );
        """
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(schema)

    # ========== 上下文分析 ==========

    def analyze_context(self, user_message: str,
                        current_task: str = "") -> ContextSignal:
        """
        分析用户消息的上下文信号
        """
        msg_lower = user_message.lower()
        signal = ContextSignal(signal_type="neutral", confidence=0.5)

        # 检测提问
        if any(q in msg_lower for q in self.QUESTION_SIGNALS):
            signal.signal_type = "question"
            signal.confidence = 0.8

        # 检测困惑
        confused_count = sum(1 for s in self.CONFUSED_SIGNALS if s in msg_lower)
        if confused_count >= 1:
            signal.signal_type = "confused"
            signal.emotional_state = "frustrated"
            signal.confidence = min(0.5 + confused_count * 0.15, 0.95)

        # 检测紧急
        if any(u in msg_lower for u in self.URGENT_SIGNALS):
            signal.emotional_state = "urgent"
            signal.confidence = min(signal.confidence + 0.2, 1.0)

        # 提取关键词（简单的中文/英文词提取）
        signal.keywords = self._extract_keywords(user_message)

        # 提取提及的工具（从消息中匹配常见技术工具）
        signal.mentioned_tools = self._extract_tools(user_message)

        # 检测新主题（如果提供了 current_task）
        if current_task:
            task_keywords = set(self._extract_keywords(current_task))
            msg_keywords = set(signal.keywords)
            overlap = len(task_keywords & msg_keywords) / max(len(task_keywords), 1)
            if overlap < 0.3 and len(msg_keywords) > 3:
                signal.signal_type = "new_topic"
                signal.confidence = max(signal.confidence, 0.6)

        return signal

    # ========== 知识匹配 ==========

    def match_knowledge(self, signal: ContextSignal) -> List[KnowledgeMatch]:
        """
        将上下文信号与知识库匹配

        Returns:
            按匹配分数降序排列的知识匹配列表
        """
        matches = []
        pages = self._get_page_index()

        for page_info in pages:
            score = 0.0
            reasons = []

            # 1. 工具实体匹配（权重最高）
            page_tools = set(page_info.get("tool_entities", []))
            mentioned_tools = set(signal.mentioned_tools)
            tool_overlap = page_tools & mentioned_tools
            if tool_overlap:
                tool_score = len(tool_overlap) / max(len(page_tools), 1)
                score += tool_score * self.MATCH_WEIGHTS["tool_match"]
                reasons.append(f"工具匹配: {', '.join(tool_overlap)}")

            # 2. 场景标签匹配（支持模糊子串匹配）
            page_scenarios = set(page_info.get("scenario_tags", []))
            msg_keywords = set(signal.keywords)
            scenario_overlap = self._fuzzy_overlap(page_scenarios, msg_keywords)
            if scenario_overlap:
                scenario_score = len(scenario_overlap) / max(len(page_scenarios), 1)
                score += scenario_score * self.MATCH_WEIGHTS["scenario_match"]
                reasons.append(f"场景匹配: {', '.join(scenario_overlap)}")

            # 3. 核心概念匹配（支持模糊子串匹配）
            page_concepts = set(page_info.get("core_concepts", []))
            concept_overlap = self._fuzzy_overlap(page_concepts, msg_keywords)
            if concept_overlap:
                concept_score = len(concept_overlap) / max(len(page_concepts), 1)
                score += concept_score * self.MATCH_WEIGHTS["keyword_match"]
                reasons.append(f"概念匹配: {', '.join(concept_overlap)}")

            # 4. 标题关键词匹配（支持模糊子串匹配）
            title_keywords = set(page_info.get("title_keywords", []))
            title_overlap = self._fuzzy_overlap(title_keywords, msg_keywords)
            if title_overlap:
                title_score = len(title_overlap) / max(len(title_keywords), 1)
                score += title_score * self.MATCH_WEIGHTS["title_match"]
                reasons.append(f"标题匹配: {', '.join(title_overlap)}")

            # 5. 形态 bonus（提问时优先问题-解决类）
            if signal.signal_type == "question" and page_info.get("form") == "问题-解决":
                score += self.MATCH_WEIGHTS["form_bonus"]
                reasons.append("提问上下文 + 问题-解决知识")

            if signal.signal_type == "confused" and page_info.get("form") in ["问题-解决", "反模式"]:
                score += self.MATCH_WEIGHTS["form_bonus"]
                reasons.append("困惑上下文 + 排错知识")

            if score >= 0.2:  # 最低匹配门槛
                # 确定优先级
                if score >= 0.75 or signal.signal_type in ["question", "confused"]:
                    priority = "high"
                elif score >= 0.55:
                    priority = "medium"
                else:
                    priority = "low"

                matches.append(KnowledgeMatch(
                    page_path=page_info["path"],
                    page_title=page_info.get("title", Path(page_info["path"]).stem),
                    match_score=round(min(score, 1.0), 3),
                    match_reason="; ".join(reasons),
                    relevant_excerpt=page_info.get("core_excerpt", "")[:200],
                    push_priority=priority,
                ))

        # 按分数降序
        matches.sort(key=lambda x: x.match_score, reverse=True)
        return matches

    # ========== 推送决策 ==========

    def _get_dynamic_threshold(self, signal: ContextSignal) -> float:
        """根据信号类型动态调整推送阈值"""
        if signal.signal_type in ("question", "confused"):
            return 0.38  # 提问/困惑时降低门槛
        elif signal.signal_type == "new_topic":
            return 0.55
        else:
            return self.PUSH_THRESHOLD

    def decide_push(self, user_message: str,
                    current_task: str = "",
                    session_id: str = "") -> PushDecision:
        """
        决定是否推送知识

        完整流程：分析上下文 → 匹配知识 → 检查冷却 → 生成推送内容
        """
        # 1. 分析上下文
        signal = self.analyze_context(user_message, current_task)

        # 2. 检查冷却
        last_push = self._get_last_push_time(session_id)
        if last_push:
            elapsed = (datetime.now(timezone.utc) - last_push).total_seconds()
            if elapsed < self.COOLDOWN_SECONDS:
                return PushDecision(
                    should_push=False,
                    reason=f"冷却中，距离上次推送 {elapsed:.0f} 秒",
                    cooldown_seconds=int(self.COOLDOWN_SECONDS - elapsed),
                )

        # 3. 低置信度不推送
        if signal.confidence < 0.4:
            return PushDecision(
                should_push=False,
                reason="上下文信号不明确，避免打扰",
            )

        # 4. 匹配知识（使用动态阈值）
        threshold = self._get_dynamic_threshold(signal)
        matches = self.match_knowledge(signal)
        top_matches = [m for m in matches if m.match_score >= threshold]

        if not top_matches:
            return PushDecision(
                should_push=False,
                reason=f"未找到匹配度足够的知识（阈值 {threshold}）",
            )

        # 限制推送数量
        top_matches = top_matches[:self.MAX_PUSH_COUNT]

        # 5. 生成推送内容
        push_content = self._generate_push_content(signal, top_matches)

        return PushDecision(
            should_push=True,
            reason=f"上下文信号: {signal.signal_type} (置信度 {signal.confidence:.2f})，"
                   f"匹配到 {len(top_matches)} 条相关知识",
            matches=top_matches,
            push_content=push_content,
        )

    def _generate_push_content(self, signal: ContextSignal,
                                matches: List[KnowledgeMatch]) -> str:
        """生成推送内容（带上下文围栏）"""
        lines = [
            "<wiki-context>",
            "",
            f"[知识库] 基于你当前{'的提问' if signal.signal_type == 'question' else '的情况'}，"
            f"推荐 {len(matches)} 条相关知识：",
            "",
        ]

        for i, match in enumerate(matches, 1):
            lines.append(f"{i}. **{match.page_title}** (相关度 {match.match_score:.0%})")
            if match.relevant_excerpt:
                excerpt = match.relevant_excerpt.replace("\n", " ")
                lines.append(f"   > {excerpt}...")
            lines.append("")

        lines.extend([
            "你可以说 **'展开第 N 条'** 查看完整内容，或者说 **'忽略'** 关闭推荐。",
            "</wiki-context>",
        ])

        return "\n".join(lines)

    # ========== 反馈记录 ==========

    def record_push(self, decision: PushDecision, session_id: str = "",
                    user_response: str = ""):
        """记录推送历史"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute(
                """INSERT INTO push_history (timestamp, trigger_context, pushed_pages,
                    user_response, session_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    decision.reason,
                    json.dumps([m.page_path for m in decision.matches], ensure_ascii=False),
                    user_response,
                    session_id,
                )
            )
            conn.commit()

    def get_push_stats(self, days: int = 7) -> Dict:
        """获取推送统计"""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:19]
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM push_history WHERE timestamp >=?", (since,)
            ).fetchone()[0]

            responses = conn.execute(
                """SELECT user_response, COUNT(*) FROM push_history
                   WHERE timestamp >? AND user_response != ''
                   GROUP BY user_response""",
                (since,)
            ).fetchall()

        return {
            "total_pushes": total,
            "response_distribution": {r[0]: r[1] for r in responses},
            "accept_rate": sum(r[1] for r in responses if r[0] in ["accept", "记住了"]) / max(total, 1),
        }

    # ========== 索引管理 ==========

    def _get_page_index(self) -> List[Dict]:
        """获取页面索引（带缓存）"""
        # 缓存 5 分钟
        if (self._page_index is not None and self._index_timestamp and
            (datetime.now(timezone.utc) - self._index_timestamp).total_seconds() < 300):
            return self._page_index

        pages = []
        if not self.inbox.exists():
            return pages

        for page in self.inbox.glob("*.md"):
            try:
                content = page.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
                body = self._extract_body(content)

                keywords = fm.get("关键词", {})
                core_excerpt = ""
                if "## 核心内容" in body:
                    parts = body.split("## 核心内容", 1)
                    if len(parts) > 1:
                        core_excerpt = parts[1].split("##")[0].strip()[:300]

                pages.append({
                    "path": str(page),
                    "title": self._extract_title(content) or page.stem,
                    "form": fm.get("类型", ""),
                    "core_concepts": keywords.get("核心概念", []) if isinstance(keywords, dict) else [],
                    "scenario_tags": keywords.get("场景标签", []) if isinstance(keywords, dict) else [],
                    "tool_entities": keywords.get("工具实体", []) if isinstance(keywords, dict) else [],
                    "title_keywords": self._extract_keywords(self._extract_title(content) or ""),
                    "core_excerpt": core_excerpt,
                })
            except Exception:
                continue

        self._page_index = pages
        self._index_timestamp = datetime.now(timezone.utc)
        return pages

    def refresh_index(self):
        """手动刷新索引"""
        self._page_index = None
        self._index_timestamp = None

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_keywords(text: str) -> List[str]:
        """提取文本关键词"""
        # 中英文词提取
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|[一-鿿]{2,}", text)

        # 对长中文词做 2-gram 拆分，增加匹配粒度（英文不做）
        expanded = []
        for w in words:
            expanded.append(w)
            # 只对纯中文且长度>4的词做 2-gram
            if len(w) > 4 and all('一' <= c <= '鿿' for c in w):
                for i in range(len(w) - 1):
                    bigram = w[i:i + 2]
                    expanded.append(bigram)

        # 过滤常见虚词
        stopwords = {"的", "了", "在", "是", "我", "有", "和", "就", "不",
                     "都", "一", "上", "也", "很", "到", "说", "要", "去",
                     "会", "着", "没有", "看", "好", "自己", "这", "那",
                     "怎么", "如何", "什么", "为什么", "哪些", "哪个",
                     "the", "and", "for", "are", "but", "not", "you",
                     "all", "can", "had", "her", "was", "one", "our"}
        # 统一小写，避免大小写不匹配
        return [w.lower() for w in expanded if w.lower() not in stopwords and len(w) > 1]

    @staticmethod
    def _extract_tools(text: str) -> List[str]:
        """从文本中提取可能的技术工具"""
        # 常见技术工具模式
        tool_patterns = [
            r"\b(Python|JavaScript|TypeScript|Java|Go|Rust|C\+\+|C#|Ruby|PHP)\b",
            r"\b(Docker|Kubernetes|K8s|AWS|GCP|Azure)\b",
            r"\b(MySQL|PostgreSQL|MongoDB|Redis|Elasticsearch)\b",
            r"\b(React|Vue|Angular|Svelte|Next\.js|Nuxt)\b",
            r"\b(Node\.js|Django|Flask|FastAPI|Spring)\b",
            r"\b(Git|GitHub|GitLab|Jenkins|CircleCI|Travis)\b",
            r"\b(asyncio|multiprocessing|threading|concurrent)\b",
            r"\b(Obsidian|Notion|Logseq|Roam)\b",
        ]

        tools = []
        for pattern in tool_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            tools.extend(matches)

        return list(set(tools))

    @staticmethod
    def _fuzzy_overlap(set_a: Set[str], set_b: Set[str]) -> Set[str]:
        """模糊集合重叠：如果 A 中的元素包含 B 中的元素（或反之），算匹配"""
        overlap = set()
        for a in set_a:
            for b in set_b:
                if a in b or b in a:
                    overlap.add(a)
                    break
        return overlap

    def _get_last_push_time(self, session_id: str = "") -> Optional[datetime]:
        """获取上次推送时间"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            if session_id:
                row = conn.execute(
                    "SELECT timestamp FROM push_history WHERE session_id=? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (session_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT timestamp FROM push_history ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()

        if row:
            return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return None

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    return yaml.safe_load(parts[1]) or {}
                except Exception:
                    pass
        return {}

    @staticmethod
    def _extract_body(content: str) -> str:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2]
        return content

    @staticmethod
    def _extract_title(content: str) -> str:
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""


# ========== 便捷函数 ==========

def check_and_push(user_message: str, wiki_base: str = None,
                   current_task: str = "", session_id: str = "") -> PushDecision:
    """便捷函数：检查并决定是否推送"""
    engine = PredictivePushEngine(wiki_base=wiki_base)
    return engine.decide_push(user_message, current_task, session_id)
