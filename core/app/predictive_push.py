# -*- coding: utf-8 -*-
"""
PredictivePush — 预测性知识推送

两层信号检测：正则关键词（<1ms）→ LLM 确认（~500ms）
冷启动：COLD 不推送，WARM 每天1条标注beta
"""

from __future__ import annotations

import logging
import re
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional



logger = logging.getLogger(__name__)
@dataclass
class PushSignal:
    """推送信号"""
    signal_type: str  # explicit_question / frustration / topic_switch / tool_mention
    confidence: float
    topic: str
    matched_text: str


@dataclass
class PushDecision:
    """推送决策"""
    should_push: bool
    page_path: str
    title: str
    reason: str
    confidence: float
    signal: Optional[PushSignal] = None


class PredictivePush:
    """预测性知识推送"""

    # Layer 1: 正则/关键词信号检测
    SIGNAL_RULES = [
        {
            "type": "explicit_question",
            "patterns": [
                r"怎么(解决|处理|办)", r"为什么.{0,5}(不|报错|失败)",
                r"如何(实现|配置|安装|部署)", r"有没有(办法|方案|替代)",
                r"(修复|解决|配置|设置|调试|报错|错误|问题|冲突)",
            ],
            "confidence": 0.8,
        },
        {
            "type": "frustration",
            "patterns": [
                r"又(挂了|崩了|报错了)", r"(搞不定|解决不了|卡住了)",
                r"(烦|郁闷|崩溃)", r"试了.{0,5}(没用|不行|还是报错)",
            ],
            "confidence": 0.6,
        },
        {
            "type": "topic_switch",
            "patterns": [
                r"(对了|顺便问一下|另外)", r"换个(话题|方向|思路)",
                r"接下来(做|聊|处理)",
            ],
            "confidence": 0.5,
        },
        {
            "type": "tool_mention",
            "patterns": [
                r"(Docker|docker|K8s|k8s|Kubernetes)", r"(Redis|redis|MySQL|mysql|PostgreSQL)",
                r"(React|Vue|Angular|Next)", r"(Nginx|nginx|Terraform|Ansible)",
                r"(codex-cli|codex|claude|kimi|openclaw)",
            ],
            "confidence": 0.6,
        },
    ]

    # 画像加权（盲点相关匹配 ×1.3）
    BLINDSPOT_BOOST = 1.3

    # 推送阈值
    PUSH_THRESHOLD = 0.7

    # 冷启动模式
    # COLD: 不推送
    # WARM: 每天1条，标注beta
    # HOT: 正常推送

    def __init__(self, wiki_base: Optional[str] = None, db_path: Optional[str] = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            from core.config import get_config
            self.wiki_base = get_config().wiki_dir

        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config
            self.DB_PATH = get_config().data_dir / "push_cache.db"
        self._init_db()
        self._topic_cache: Dict[str, float] = {}  # topic → last_push_time

    def _init_db(self):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    page_path TEXT,
                    confidence REAL,
                    signal_type TEXT,
                    pushed_at REAL NOT NULL,
                    user_action TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ph_topic
                ON push_history(topic, pushed_at)
            """)

    def detect_and_decide(self, user_input: str,
                          context: Optional[Dict] = None) -> List[PushDecision]:
        """
        检测用户输入中的推送信号并做出推送决策。

        Args:
            user_input: 用户输入文本
            context: 上下文（working_dir, session_id 等）

        Returns:
            推送决策列表
        """
        # 冷启动检查
        mode = self._get_mode()
        if mode == "COLD":
            return []

        # Layer 1: 正则检测
        signals = self._detect_signals(user_input)
        if not signals:
            return []

        decisions = []
        for signal in signals:
            # 同主题 30 分钟缓存
            if self._is_topic_cached(signal.topic):
                continue

            # 查找相关 Wiki 页面
            page = self._find_related_page(signal)
            if not page:
                continue

            # 计算最终置信度
            confidence = signal.confidence

            # 画像加权
            if self._is_blindspot_related(signal.topic):
                confidence *= self.BLINDSPOT_BOOST

            # 冷启动 WARM 模式：限制每天1条
            if mode == "WARM":
                if self._count_today_pushes() >= 1:
                    continue

            should_push = confidence >= self.PUSH_THRESHOLD

            decisions.append(PushDecision(
                should_push=should_push,
                page_path=page.get("path") or page.get("page_path", ""),
                title=page.get("title", ""),
                reason=f"检测到 {signal.signal_type} 信号：{signal.matched_text[:30]}",
                confidence=confidence,
                signal=signal,
            ))

        # 限制每批最多 3 条
        decisions = [d for d in decisions if d.should_push][:3]

        # 记录推送历史
        for d in decisions:
            self._record_push(d)
            self._topic_cache[d.title] = time.time()

        return decisions

    def record_user_action(self, push_id: int, action: str) -> None:
        """记录用户对推送的反馈（accept / ignore）"""
        from core.app.application_hub import PushPenaltyTracker
        tracker = PushPenaltyTracker()
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                "UPDATE push_history SET user_action = ? WHERE id = ?",
                (action, push_id),
            )
            cursor = conn.execute(
                "SELECT topic FROM push_history WHERE id = ?",
                (push_id,),
            )
            row = cursor.fetchone()
            if row:
                topic = row[0]
                if action == "ignore":
                    tracker.record_ignore(topic)
                elif action == "accept":
                    tracker.record_accept(topic)
                    # 记录训练样本：用户采纳推送 → 强正样本（profile 维度）
                    try:
                        from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
                        AdaptiveScorerV2.enqueue_training_sample(
                            session_id=f"push-{push_id}",
                            dimension="profile",
                            features={"topic": topic, "action": "accept", "tool": "predictive_push"},
                            expected_score=0.9,
                            source="predictive_push_accept",
                        )
                    except Exception:
                        pass

    def _detect_signals(self, text: str) -> List[PushSignal]:
        """Layer 1: 正则关键词检测"""
        signals = []
        for rule in self.SIGNAL_RULES:
            for pattern in rule["patterns"]:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    # 提取主题词
                    topic = self._extract_topic(text, match)
                    signals.append(PushSignal(
                        signal_type=rule["type"],
                        confidence=rule["confidence"],
                        topic=topic,
                        matched_text=match.group(0),
                    ))
        return signals

    def _extract_topic(self, text: str, match: re.Match) -> str:
        """从匹配上下文提取主题词

        优先级：
        1. code/tool token（含连字符，如 codex-cli）
        2. 大写环境变量（如 OPENAI_BASE_URL）
        3. 英文技术词（>=3 字符）
        4. 中文名词短语（>=2 字符）
        5. 回退到匹配文本本身
        """
        start = max(0, match.start() - 30)
        end = min(len(text), match.end() + 30)
        context = text[start:end].strip()

        # 1. code/tool token（连字符/下划线/点号）
        code_tokens = re.findall(r'[A-Za-z][A-Za-z0-9_\-\.]*-[A-Za-z0-9_\-\.]+', context)
        if code_tokens:
            return max(code_tokens, key=len)

        # 2. 大写环境变量（全大写 + 下划线）
        env_vars = re.findall(r'[A-Z][A-Z0-9_]+', context)
        if env_vars:
            return max(env_vars, key=len)

        # 3. 英文技术词（>=3 字符，排除常见介词）
        stopwords = {"the", "and", "for", "how", "can", "you", "use", "with", "what", "why", "when", "where", "which", "this", "that", "have", "has", "had", "not", "but", "from", "they", "she", "him", "her", "his", "are", "was", "were", "been", "being", "did", "does", "doing", "will", "would", "could", "should", "may", "might", "must", "shall"}
        eng_words = re.findall(r'[A-Za-z][A-Za-z0-9_]*', context)
        eng_words = [w for w in eng_words if len(w) >= 3 and w.lower() not in stopwords]
        if eng_words:
            return max(eng_words, key=len)

        # 4. 中文词（>=2 字符）
        cn_words = re.findall(r'[一-鿿]{2,}', context)
        if cn_words:
            return cn_words[0]

        return match.group(0)

    def _find_related_page(self, signal: PushSignal) -> Optional[Dict]:
        """查找与信号相关的 Wiki 页面（优先 context_aware_search，KG 辅助，再回退文件名）

        过滤：pending-verification 和低置信度(<0.5)页面不进入主动推送。
        Relevance Gate：top result score >= 0.55，且 title/snippet 必须包含主题 token。
        """
        topic_lower = signal.topic.lower()
        topic_tokens = set(re.findall(r'[a-z0-9]+', topic_lower))

        def _relevance_gate(result) -> bool:
            """检查搜索结果是否与主题真正相关"""
            # score 阈值
            score = getattr(result, "score", 0.0) or result.get("score", 0.0)
            if score < 0.55:
                return False
            # title/snippet 必须包含至少一个主题 token
            title = (getattr(result, "title", "") or result.get("title", "")).lower()
            snippet = (getattr(result, "snippet", "") or result.get("content", "")).lower()
            combined = title + " " + snippet
            matched_tokens = [t for t in topic_tokens if t in combined and len(t) >= 2]
            if not matched_tokens:
                return False
            return True

        # 1. 优先 ContextAwareSearch（相关性更可靠）
        try:
            from core.app.context_search import ContextAwareSearch
            searcher = ContextAwareSearch(wiki_base=str(self.wiki_base))
            results = searcher.search(signal.topic, limit=5)
            for r in results:
                if _relevance_gate(r) and self._is_pushable({"verification": getattr(r, "verification", ""), "confidence": getattr(r, "confidence", 0.5)}):
                    return {
                        "path": r.page_path,
                        "title": r.title,
                        "confidence": r.confidence,
                        "score": r.score,
                    }
        except Exception:
            pass

        # 2. KG 辅助召回（作为补充，但不单独决定推送）
        try:
            from core.kia.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            results = kg.search(signal.topic, limit=5)
            for r in results:
                # KG 结果需要额外验证：文件名或标题包含主题 token
                title = (r.get("title", "") or r.get("entity_name", "")).lower()
                if not any(t in title for t in topic_tokens if len(t) >= 2):
                    continue
                if self._is_pushable(r):
                    return r
        except Exception:
            pass

        # 3. 回退：文件名搜索（保守策略）
        for md_file in self.wiki_base.rglob("*.md"):
            if os.path.islink(md_file):
                continue
            stem_lower = md_file.stem.lower()
            # 文件名必须精确包含主题词（不是子串匹配）
            if signal.topic.lower() in stem_lower:
                return {"path": str(md_file.relative_to(self.wiki_base)), "title": md_file.stem}

        return None

    def _is_pushable(self, page: Dict) -> bool:
        """判断页面是否适合主动推送"""
        # 过滤 pending-verification
        if page.get("verification") == "pending-verification":
            return False
        # 过滤低置信度（系统默认 0.4，阈值不宜过高）
        if page.get("confidence", 0.5) < 0.3:
            return False
        return True

    def _is_topic_cached(self, topic: str) -> bool:
        """同主题 30 分钟缓存，定期清理过期条目防止内存泄漏"""
        now = time.time()
        # 每 100 次检查清理一次过期缓存
        if len(self._topic_cache) > 100 and getattr(self, '_cache_check_count', 0) % 100 == 0:
            cutoff = now - 1800
            self._topic_cache = {k: v for k, v in self._topic_cache.items() if v > cutoff}
        self._cache_check_count = getattr(self, '_cache_check_count', 0) + 1
        last_push = self._topic_cache.get(topic, 0)
        return (now - last_push) < 1800

    def _is_blindspot_related(self, topic: str) -> bool:
        """检查主题是否与已知盲点相关"""
        try:
            import sqlite3
            from core.config import get_config
            db_path = get_config().data_dir / "blindspots.db"
            if not db_path.exists():
                return False
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                cursor = conn.execute(
                    "SELECT 1 FROM blindspots WHERE topic = ? AND status NOT IN ('resolved', 'ignored')",
                    (topic,),
                )
                return cursor.fetchone() is not None
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at predictive_push.py", exc_info=True)
            return False

    def _get_mode(self) -> str:
        """获取冷启动模式"""
        try:
            from core.persona.psyche import get_signal_store
            stats = get_signal_store().get_signal_stats(days=90)
            total = sum(v for v in stats.values() if v > 0)
            if total < 10:
                return "COLD"
            elif total < 50:
                return "WARM"
            else:
                return "HOT"
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at predictive_push.py", exc_info=True)
            return "COLD"

    def _count_today_pushes(self) -> int:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM push_history WHERE pushed_at >= ?",
                (today_start.timestamp(),),
            )
            return cursor.fetchone()[0]

    def _record_push(self, decision: PushDecision) -> None:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                INSERT INTO push_history (topic, page_path, confidence, signal_type, pushed_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                decision.signal.topic if decision.signal else "",
                decision.page_path,
                decision.confidence,
                decision.signal.signal_type if decision.signal else "",
                time.time(),
            ))
