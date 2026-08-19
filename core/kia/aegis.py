"""
In-process Guard - 执行中守护

执行过程中检测风险点，三级策略：
- 轻微偏差：静默记录，任务完成后汇总报告
- 中等偏差：AI 回复中自然融入提醒
- 严重偏差：打断用户，明确要求确认

避免打断用户思路，非侵入式保护。
"""

# Aegis — 宙斯神盾 — 执行中守护，KIA 闭环的实时防护
# 原模块: in_process_guard.py


import re
import sqlite3
from datetime import datetime
from typing import Any, List, Dict, Optional, Tuple

from .aegis_models import ExecutionContext, GuardAlert, GuardLevel, GuardSession
from .prophasis import ChecklistItem, LoadedKnowledge
from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient


import logging

logger = logging.getLogger(__name__)

# 模块级知识缓存：避免类级可变默认值陷阱，同时保持跨实例共享
_KNOWLEDGE_CACHE: Dict[Tuple[str, str], Tuple[LoadedKnowledge, datetime]] = {}
_KNOWLEDGE_TTL_SECONDS: int = 60

# 模块级 embedding client health_check 结果缓存
_EMBEDDING_CLIENT_CHECK: Tuple[Optional[Any], datetime] = (None, datetime.min)
_EMBEDDING_CLIENT_CHECK_TTL_SECONDS: int = 60

# ==================== SmartMatcher 三层匹配引擎 ====================


class SmartMatcher:
    """三层级联匹配引擎

    【E14 三层匹配引擎补全】
    层级1 — 精确匹配：文本完全相等（最高置信度）
    层级2 — 关键词匹配：子串包含（当前已有）
    层级3 — 语义匹配：本地词袋 Jaccard（零 API 成本），
             embedding client 可用时升级为向量余弦相似度
    """

    def __init__(self, semantic_threshold: float = 0.65, embedding_client=None):
        self.semantic_threshold = semantic_threshold
        self.embedding_client = embedding_client
        # 未传入 embedding client 时尝试懒加载，使 SmartMatcher 可独立使用
        if self.embedding_client is None:
            try:
                self.embedding_client = InProcessGuard._load_embedding_client()
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.debug("[SmartMatcher] embedding client 懒加载失败", exc_info=True)
        self.negation_words = {
            "不要",
            "别",
            "勿",
            "无需",
            "不用",
            "禁止",
            "避免",
            "不能",
            "不要直接",
        }
        self.question_markers = {
            "?",
            "？",
            "怎么",
            "如何",
            "为什么",
            "会怎样",
            "是什么意思",
            "是否",
        }
        self.command_verbs = {
            "删除",
            "清空",
            "执行",
            "运行",
            "部署",
            "发布",
            "修改",
            "覆盖",
            "drop",
            "truncate",
            "rm",
        }

    def match_exact(self, text: str, candidates: List[str]) -> Optional[Tuple[str, float]]:
        """精确匹配：文本与候选完全相等"""
        text_lower = text.strip().lower()
        for cand in candidates:
            if cand.strip().lower() == text_lower:
                return cand, 1.0
        return None

    def match_keyword(self, text: str, keywords: List[str]) -> Optional[Tuple[str, float]]:
        """关键词匹配：子串包含"""
        text_lower = text.lower()
        for kw in keywords:
            kw_lower = kw.lower()
            idx = text_lower.find(kw_lower)
            if idx >= 0:
                score = self._contextual_score(text, kw, idx, 0.85)
                if score >= 0.5:
                    return kw, score
        return None

    def _keyword_similarity(self, text: str, reference: str) -> float:
        """计算两个文本的词袋 Jaccard 相似度"""
        text_words = set(re.findall(r"[\w\u4e00-\u9fa5]+", text.lower()))
        ref_words = set(re.findall(r"[\w\u4e00-\u9fa5]+", reference.lower()))
        if not text_words or not ref_words:
            return 0.0
        intersection = text_words & ref_words
        union = text_words | ref_words
        return len(intersection) / len(union) if union else 0.0

    def _embedding_semantic(
        self,
        text: str,
        references: List[str],
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> Optional[Tuple[str, float]]:
        """
        使用 embedding 向量计算语义相似度。
        失败时返回 None，由调用方决定是否回退。
        """
        if not self.embedding_client:
            return None
        try:
            all_texts = [text] + references
            if isinstance(self.embedding_client, SiliconFlowEmbeddingClient):
                effective_scope = subject_scope
                if effective_scope is None:
                    from core.telemetry.prompt_call_log import current_model_call_run

                    if current_model_call_run() is None:
                        effective_scope = ("source", "in_process_guard")
                entry_subject_scopes = [
                    (effective_scope,) if effective_scope is not None else ()
                    for _ in all_texts
                ]
                embeddings = self.embedding_client.embed(
                    all_texts,
                    subject_scopes=entry_subject_scopes,
                )
            else:
                # In-process test/local matchers are non-provider clients and
                # deliberately retain their compact compatibility interface.
                embeddings = self.embedding_client.embed(all_texts)
            if not embeddings or embeddings[0] is None:
                return None
            query_emb = embeddings[0]
            candidate_embs = embeddings[1:]

            import math

            q_norm = math.sqrt(sum(x * x for x in query_emb))
            if q_norm == 0:
                return None

            best_ref = None
            best_score = 0.0
            for ref, emb in zip(references, candidate_embs):
                if emb is None:
                    continue
                v_norm = math.sqrt(sum(x * x for x in emb))
                if v_norm == 0:
                    continue
                dot = sum(x * y for x, y in zip(query_emb, emb))
                score = dot / (q_norm * v_norm)
                if score > best_score:
                    best_score = score
                    best_ref = ref

            if best_ref and best_score >= self.semantic_threshold:
                return best_ref, best_score
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[SmartMatcher] embedding 语义匹配失败，回退到 Jaccard", exc_info=True)
        return None

    def match_semantic(
        self,
        text: str,
        references: List[str],
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> Optional[Tuple[str, float]]:
        """
        语义匹配：先尝试零成本的词袋 Jaccard；
        未命中且 embedding client 可用时，使用向量余弦相似度。
        """
        # 快速路径：本地 Jaccard 命中则直接返回，避免 API 调用
        best_ref = None
        best_score = 0.0
        for ref in references:
            score = self._keyword_similarity(text, ref)
            if score > best_score and score >= self.semantic_threshold:
                best_score = score
                best_ref = ref
        if best_ref:
            return best_ref, best_score

        # 慢速路径：embedding 语义相似度（处理同义/近义）
        if self.embedding_client and len(text) >= 20:
            if subject_scope is None:
                emb_result = self._embedding_semantic(text, references)
            else:
                emb_result = self._embedding_semantic(
                    text,
                    references,
                    subject_scope=subject_scope,
                )
            if emb_result:
                return emb_result

        return None

    def match_three_tier(
        self,
        text: str,
        exact_candidates: List[str] | None = None,
        keywords: List[str] | None = None,
        semantic_refs: List[str] | None = None,
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> Optional[Dict]:
        """三层级联匹配：依次尝试精确 → 关键词 → 语义"""
        # Layer 1: Exact
        if exact_candidates:
            result = self.match_exact(text, exact_candidates)
            if result:
                return {"layer": 1, "type": "exact", "match": result[0], "score": result[1]}

        # Layer 2: Keyword
        if keywords:
            result = self.match_keyword(text, keywords)
            if result:
                return {"layer": 2, "type": "keyword", "match": result[0], "score": result[1]}

        # Layer 3: Semantic
        if semantic_refs:
            if subject_scope is None:
                result = self.match_semantic(text, semantic_refs)
            else:
                result = self.match_semantic(
                    text,
                    semantic_refs,
                    subject_scope=subject_scope,
                )
            if result:
                return {"layer": 3, "type": "semantic", "match": result[0], "score": result[1]}

        return None

    def _contextual_score(self, text: str, keyword: str, pos: int, base_score: float) -> float:
        window_start = max(0, pos - 10)
        window_end = min(len(text), pos + len(keyword) + 10)
        window = text[window_start:window_end]
        prefix = text[max(0, pos - 8) : pos]
        score = base_score

        if any(word in prefix for word in self.negation_words):
            score *= 0.2
        if any(marker in text for marker in self.question_markers):
            score *= 0.45
        if self._is_in_code_block(text, pos) or self._is_quoted(text, pos):
            score *= 0.5
        keyword_lower = keyword.lower()
        if any(verb in window.lower().replace(keyword_lower, "") for verb in self.command_verbs):
            score = min(1.0, score + 0.1)
        return score

    @staticmethod
    def _is_in_code_block(text: str, pos: int) -> bool:
        return text[:pos].count("```") % 2 == 1

    @staticmethod
    def _is_quoted(text: str, pos: int) -> bool:
        before = text[:pos]
        return (
            before.count("`") % 2 == 1
            or before.count('"') % 2 == 1
            or before.count("“") > before.count("”")
        )


class DuplicateWorkDetector:
    """重复工作检测器

    检测用户是否在做之前已经做过/讨论过的工作。
    基于消息指纹 + 关键词重叠 + 语义相似度（embedding 可用时升级为向量语义）。
    """

    def __init__(self, history_messages: List[str] | None = None, embedding_client=None):
        self.history = history_messages or []
        self.matcher = SmartMatcher(semantic_threshold=0.55, embedding_client=embedding_client)

    def _fingerprint(self, text: str) -> str:
        """生成文本指纹"""
        cleaned = re.sub(r"[^\w\u4e00-\u9fa5]", "", text.lower())
        return cleaned[:100]

    def is_duplicate(self, message: str, threshold: float = 0.70) -> Tuple[bool, float, str]:
        """
        检测消息是否与历史记录重复

        Returns:
            (是否重复, 相似度, 原因)
        """
        if not self.history:
            return False, 0.0, "No history"

        msg_fp = self._fingerprint(message)

        for hist in self.history:
            hist_fp = self._fingerprint(hist)

            # 1. 指纹精确匹配
            if msg_fp == hist_fp and len(msg_fp) > 10:
                return True, 1.0, "Exact fingerprint match with history"

            # 2. 语义相似度
            result = self.matcher.match_semantic(message, [hist])
            if result:
                score = result[1]
                if score >= threshold:
                    return True, score, f"Semantic similarity {score:.2f} with history"

        # 3. 关键词重叠率（快速过滤）
        msg_words = set(re.findall(r"[\w\u4e00-\u9fa5]+", message.lower()))
        if len(msg_words) < 3:
            return False, 0.0, "Too few words"

        best_overlap = 0.0
        best_hist = ""
        for hist in self.history:
            hist_words = set(re.findall(r"[\w\u4e00-\u9fa5]+", hist.lower()))
            if not hist_words:
                continue
            overlap = len(msg_words & hist_words) / len(msg_words | hist_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_hist = hist[:50]

        if best_overlap >= threshold:
            return True, best_overlap, f"Keyword overlap {best_overlap:.2f} with: {best_hist}..."

        return False, best_overlap, "No significant overlap"

    def add_message(self, message: str):
        """添加消息到历史"""
        self.history.append(message)
        # 限制历史长度，防止内存膨胀
        if len(self.history) > 1000:
            self.history = self.history[-500:]


class InProcessGuard:
    """执行中守护"""

    DEFAULT_MAX_ANALYSIS_TURNS_WITHOUT_ACTION = 2
    DEFAULT_MAX_REPEATED_READS_PER_TARGET = 2

    # 严重偏差关键词（触发 INTERRUPT）
    CRITICAL_KEYWORDS = {
        "coding": ["rm -rf", "drop table", "delete from", "truncate", "os.system"],
        "marketing": ["全部预算", "all budget", "无门槛", "无限制"],
        "analysis": ["删除数据", "修改原始数据", "造假"],
        "strategy": ["all in", "全部押注", "孤注一掷"],
    }

    # 向后兼容：引用模块级缓存字典（避免类级可变默认值陷阱）
    _knowledge_cache = _KNOWLEDGE_CACHE

    @classmethod
    def _get_cached_knowledge(cls, task_type: str, subtype: str) -> Optional[LoadedKnowledge]:
        """从缓存获取知识，TTL 过期返回 None。"""
        key = (task_type, subtype)
        cached = _KNOWLEDGE_CACHE.get(key)
        if not cached:
            return None
        knowledge, cached_at = cached
        if (datetime.now() - cached_at).total_seconds() > _KNOWLEDGE_TTL_SECONDS:
            _KNOWLEDGE_CACHE.pop(key, None)
            return None
        return knowledge

    @classmethod
    def _set_cached_knowledge(cls, knowledge: LoadedKnowledge) -> None:
        """将知识写入缓存。"""
        _KNOWLEDGE_CACHE[(knowledge.task_type, knowledge.subtype)] = (knowledge, datetime.now())

    @classmethod
    def clear_knowledge_cache(cls) -> None:
        """手动清除知识缓存（测试/调试用）。"""
        _KNOWLEDGE_CACHE.clear()

    @classmethod
    def from_task_type(cls, task_type: str, subtype: str = "") -> "InProcessGuard":  # noqa: Vulture - guard factory API.
        """按 task_type 创建 Guard，自动使用缓存的 knowledge 或从文件系统加载。"""
        # 先尝试缓存
        knowledge = cls._get_cached_knowledge(task_type, subtype)
        if knowledge is None:
            # 从文件系统加载（通过 PreFlightInjector）
            try:
                from core.kia.prophasis import PreFlightInjector
                from core.kia.kairos import TimeWindow, TimeWindowType

                injector = PreFlightInjector()
                time_window = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
                knowledge = injector.inject(task_type, subtype, time_window, "")
                if knowledge:
                    cls._set_cached_knowledge(knowledge)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.warning("[InProcessGuard] 从文件系统加载知识失败，使用空清单", exc_info=True)
                knowledge = None
        return cls(knowledge)

    def __init__(self, knowledge: Optional[LoadedKnowledge] = None):
        self.session: Optional[GuardSession] = None
        self.embedding_client = self._load_embedding_client()
        self.smart_matcher = SmartMatcher(embedding_client=self.embedding_client)
        self.duplicate_detector = DuplicateWorkDetector(embedding_client=self.embedding_client)
        self.contextual_mode = "normal"  # normal/exploration/execution/fatigue/urgency
        self.session_messages: List[str] = []  # 记录session消息用于情境推断
        self._analysis_loop_options = self._load_analysis_loop_options()
        if knowledge:
            self.start_session(knowledge)

    @staticmethod
    def _positive_int_option(value: Any, default: int) -> Tuple[int, str]:
        try:
            if isinstance(value, bool):
                raise ValueError("bool is not a positive integer threshold")
            converted = int(value)
            if converted < 1:
                raise ValueError("threshold must be >= 1")
            return converted, "config"
        except (TypeError, ValueError):
            return default, "default"

    def _load_analysis_loop_options(self) -> Dict[str, Any]:
        defaults = {
            "enabled": True,
            "max_analysis_turns_without_action": self.DEFAULT_MAX_ANALYSIS_TURNS_WITHOUT_ACTION,
            "max_repeated_reads_per_target": self.DEFAULT_MAX_REPEATED_READS_PER_TARGET,
            "threshold_source": "default",
        }
        try:
            from core.config import get_config

            cfg = get_config()
            analysis_threshold, analysis_source = self._positive_int_option(
                cfg.get(
                    "guard.analysis_loop.max_analysis_turns_without_action",
                    self.DEFAULT_MAX_ANALYSIS_TURNS_WITHOUT_ACTION,
                ),
                self.DEFAULT_MAX_ANALYSIS_TURNS_WITHOUT_ACTION,
            )
            reads_threshold, reads_source = self._positive_int_option(
                cfg.get(
                    "guard.analysis_loop.max_repeated_reads_per_target",
                    self.DEFAULT_MAX_REPEATED_READS_PER_TARGET,
                ),
                self.DEFAULT_MAX_REPEATED_READS_PER_TARGET,
            )
            threshold_source = (
                "config" if analysis_source == "config" and reads_source == "config" else "default"
            )
            return {
                "enabled": bool(cfg.get("guard.analysis_loop.enabled", True)),
                "max_analysis_turns_without_action": analysis_threshold,
                "max_repeated_reads_per_target": reads_threshold,
                "threshold_source": threshold_source,
                "analysis_threshold_source": analysis_source,
                "reads_threshold_source": reads_source,
            }
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[InProcessGuard] analysis loop 配置加载失败，使用默认阈值", exc_info=True)
            return defaults

    def _analysis_loop_metadata(
        self,
        *,
        threshold_kind: str,
        threshold_value: int,
        current_count: int,
    ) -> Dict[str, Any]:
        source_key = {
            "max_analysis_turns_without_action": "analysis_threshold_source",
            "max_repeated_reads_per_target": "reads_threshold_source",
        }.get(threshold_kind)
        return {
            "threshold_kind": threshold_kind,
            "threshold_source": self._analysis_loop_options.get(
                source_key or "threshold_source",
                self._analysis_loop_options.get("threshold_source", "default"),
            ),
            "threshold_value": threshold_value,
            "current_count": current_count,
        }

    @staticmethod
    def _load_embedding_client():
        """懒加载 embedding client；失败时返回 None，不影响守护工作。

        health_check() 结果缓存 60 秒，避免每次创建 Guard 都发起网络探测。
        不可用结果也会被缓存，防止 unavailable client 导致反复探测。
        """
        global _EMBEDDING_CLIENT_CHECK
        try:
            from core.config import get_config

            cfg = get_config()
            if not cfg.get("embedding.enabled", True):
                return None
            from core.embeddings.siliconflow_client import get_embedding_client

            client = get_embedding_client()
            if client is None:
                return None

            cached_client, cached_at = _EMBEDDING_CLIENT_CHECK
            now = datetime.now()
            # 缓存命中：同一 client 且未过期；None 表示上次探测为不可用
            if cached_client is client or cached_client is None:
                elapsed = (now - cached_at).total_seconds()
                if elapsed < _EMBEDDING_CLIENT_CHECK_TTL_SECONDS:
                    return cached_client

            hc = client.health_check()
            if hc.get("available", False):
                _EMBEDDING_CLIENT_CHECK = (client, now)
                return client
            else:
                # 缓存不可用结果，避免反复 health_check
                _EMBEDDING_CLIENT_CHECK = (None, now)
                return None
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("[InProcessGuard] embedding client 加载失败", exc_info=True)
        return None

    def start_session(self, knowledge: LoadedKnowledge):
        """开始守护会话"""
        self.session = GuardSession(  # type: ignore[assignment]
            task_type=knowledge.task_type, subtype=knowledge.subtype, checklist=knowledge.checklist
        )
        self.session_messages = []
        self.contextual_mode = "normal"
        self._analysis_loop_options = self._load_analysis_loop_options()
        # 重置重复检测器历史（保留 embedding client）
        self.duplicate_detector = DuplicateWorkDetector(embedding_client=self.embedding_client)
        # 会话内分析计数（用于检测分析瘫痪）
        self._analysis_turn_count = 0
        self._tool_read_counts: Dict[str, int] = {}
        InProcessGuard._set_cached_knowledge(knowledge)

    def _infer_contextual_mode(self, user_message: str) -> str:
        """
        推断当前情境模式。

        Returns:
            normal / exploration / execution / fatigue / urgency
        """
        content = user_message.lower()
        all_text = (
            " ".join(m.lower() for m in self.session_messages[-5:])
            if self.session_messages
            else content
        )

        # 疲劳检测
        fatigue_signals = ["累了", "困了", "先这样", "明天再说", "懒得", "没精力"]
        if any(s in all_text for s in fatigue_signals):
            return "fatigue"

        # 紧急检测
        urgency_signals = ["快点", "着急", "马上", "立刻", "deadline", "今晚必须", "急"]
        if any(s in content for s in urgency_signals):
            return "urgency"

        # 探索模式检测
        exploration_signals = ["如果", "能不能", "试试", "还有其他", "发散", "假设", "也许"]
        if any(s in content for s in exploration_signals):
            return "exploration"

        # 执行模式检测
        execution_signals = ["开始吧", "执行", "推进", "下一步", "继续", "就这个", "确定"]
        if any(s in content for s in execution_signals):
            return "execution"

        return "normal"

    def _adjust_level_by_context(self, level: GuardLevel, mode: str) -> GuardLevel:
        """根据情境调整守护级别"""
        if mode == "fatigue":
            # 疲劳时降低打扰
            if level == GuardLevel.INTERRUPT:
                return GuardLevel.HINT
            elif level == GuardLevel.HINT:
                return GuardLevel.SILENT
        elif mode == "urgency":
            # 紧急时只保留严重告警
            if level == GuardLevel.HINT:
                return GuardLevel.SILENT
        elif mode == "exploration":
            # 探索模式允许更多试错
            if level == GuardLevel.INTERRUPT:
                return GuardLevel.HINT
        elif mode == "execution":
            # 执行模式减少干扰
            if level == GuardLevel.HINT:
                return GuardLevel.SILENT

        return level

    # 通用高风险关键词（不依赖 checklist，作为兜底规则）
    _DEFAULT_CRITICAL_KEYWORDS = [
        "删除生产",
        "删除数据库",
        "drop database",
        "drop table",
        "rm -rf",
        "rm -rf /",
        "覆盖生产",
        "truncate",
        "delete from",
        "git push --force",
        "terraform apply",
        "kubectl delete",
        "密钥",
        "password",
        "token",
        "api key",
        "secret",
        "不可逆",
        "无法回滚",
        "未测试",
        "直接上线",
    ]

    def check(
        self,
        user_message: str,
        ai_response: str = "",
        context: Optional[Dict] = None,
        execution_context: Optional[ExecutionContext] = None,
    ) -> Optional[GuardAlert]:
        """
        检测当前对话是否触及风险点

        Args:
            user_message: 用户消息
            ai_response: AI 回复（如果有）
            context: 用户操作上下文（current_file, current_command, git_status）

        Returns:
            GuardAlert 或 None
        """
        combined = (user_message + " " + ai_response).lower()

        # 0. 默认高风险规则检查（不依赖 checklist/session）
        alert = self._check_default_critical(combined, execution_context)
        if alert:
            return alert

        # 1.5 上下文语义风险检查（不依赖 checklist）
        ctx_alert = self._check_context_risk(context, user_message)
        if ctx_alert:
            ctx_alert.execution_context = execution_context
            return ctx_alert

        if not self.session or not self.session.checklist:
            return None

        # 记录消息用于情境推断
        self.session_messages.append(user_message)

        # 推断当前情境模式
        self.contextual_mode = self._infer_contextual_mode(user_message)

        # 1. 先检查严重偏差
        critical = self._check_critical(user_message, ai_response)
        if critical:
            # 严重偏差不受情境模式影响，始终告警
            critical.execution_context = execution_context
            return critical

        # 2. 重复工作检测（SmartMatcher Layer 3 语义匹配）
        alert = self._check_duplicate_work(user_message, execution_context)
        if alert:
            return alert

        # 3. 检查 checklist 中的风险点（三层匹配引擎）
        alert = self._check_checklist_matches(user_message, ai_response, execution_context)
        if alert:
            return alert

        # 5. 思考循环检测（不依赖 checklist，基于会话状态）
        loop_alert = self._check_thinking_loop(user_message, ai_response, context=context)
        if loop_alert:
            loop_alert.execution_context = execution_context
            return loop_alert

        return None

    def _check_default_critical(
        self,
        combined_text: str,
        execution_context: Optional[ExecutionContext],
    ) -> Optional[GuardAlert]:
        """默认高风险关键词检查（不依赖 checklist/session）。"""
        for kw in self._DEFAULT_CRITICAL_KEYWORDS:
            if kw.lower() in combined_text:
                alert = GuardAlert(
                    level=GuardLevel.INTERRUPT,
                    checklist_item=ChecklistItem(
                        item="高风险操作检测", source="system", severity="critical"
                    ),
                    triggered_by="system",
                    trigger_text=kw,
                    suggestion=f"⚠️ 检测到高风险操作关键词「{kw}」，请确认是否继续？",
                )
                alert.execution_context = execution_context
                return alert
        return None

    def _check_duplicate_work(
        self,
        user_message: str,
        execution_context: Optional[ExecutionContext],
    ) -> Optional[GuardAlert]:
        """重复工作检测（SmartMatcher Layer 3 语义匹配）。"""
        is_dup, dup_score, dup_reason = self.duplicate_detector.is_duplicate(user_message)
        self.duplicate_detector.add_message(user_message)
        if is_dup and dup_score >= 0.80:
            alert = GuardAlert(
                level=GuardLevel.HINT,
                checklist_item=ChecklistItem(
                    item="重复工作提醒", source="system", severity="medium"
                ),
                triggered_by="user",
                trigger_text=user_message[:100],
                suggestion=f"💡 检测到可能与之前工作重复（相似度 {dup_score:.0%}）：{dup_reason}",
            )
            alert.execution_context = execution_context
            return alert
        return None

    def _check_checklist_matches(
        self,
        user_message: str,
        ai_response: str,
        execution_context: Optional[ExecutionContext],
    ) -> Optional[GuardAlert]:
        """检查 checklist 中的风险点（三层匹配引擎）。"""
        if not self.session:
            return None
        interrupted_items = {
            a.checklist_item.item
            for a in self.session.triggered_alerts
            if a.level == GuardLevel.INTERRUPT
        }
        for item in self.session.checklist:
            # 跳过已触发的严重项
            if item.item in interrupted_items:
                continue

            # 检查用户消息中的触发关键词（三层匹配）
            if item.trigger_keywords:
                match_result = self._match_three_tier(user_message, item.trigger_keywords)
                if match_result:
                    level = self._determine_level(item, "user")
                    # 根据情境调整级别
                    level = self._adjust_level_by_context(level, self.contextual_mode)
                    # 语义匹配降低一级（减少误报）
                    if match_result.get("layer") == 3 and level == GuardLevel.INTERRUPT:
                        level = GuardLevel.HINT

                    alert = GuardAlert(
                        level=level,
                        checklist_item=item,
                        triggered_by="user",
                        trigger_text=match_result["match"],
                        suggestion=self._generate_suggestion(item),
                    )
                    alert.execution_context = execution_context
                    self._record_alert(alert)
                    return alert

            # 检查 AI 回复中的风险模式
            if ai_response and item.risk_patterns:
                match_result = self._match_three_tier(ai_response, item.risk_patterns)
                if match_result:
                    level = self._determine_level(item, "ai")
                    # 根据情境调整级别
                    level = self._adjust_level_by_context(level, self.contextual_mode)
                    # 语义匹配降低一级
                    if match_result.get("layer") == 3 and level == GuardLevel.INTERRUPT:
                        level = GuardLevel.HINT

                    alert = GuardAlert(
                        level=level,
                        checklist_item=item,
                        triggered_by="ai",
                        trigger_text=match_result["match"],
                        suggestion=self._generate_suggestion(item),
                    )
                    alert.execution_context = execution_context
                    self._record_alert(alert)
                    return alert

        return None

    def check_silent(self, user_message: str, ai_response: str = "") -> List[Dict]:
        """
        静默检测（不返回告警，只记录到内部日志）
        用于轻微偏差的批量检测

        Returns:
            记录列表
        """
        records = []  # type: ignore[var-annotated]
        if not self.session or not self.session.checklist:
            return records

        for item in self.session.checklist:
            # 只处理轻微级别的项
            if item.severity not in ["low", "medium"]:
                continue

            matched = None
            if item.trigger_keywords:
                result = self._match_three_tier(user_message, item.trigger_keywords)
                if result:
                    matched = result["match"]
                    result.get("layer", 2)
            if not matched and ai_response and item.risk_patterns:
                result = self._match_three_tier(ai_response, item.risk_patterns)
                if result:
                    matched = result["match"]
                    result.get("layer", 2)

            if matched:
                record = {
                    "item": item.item,
                    "severity": item.severity,
                    "trigger": matched,
                    "timestamp": datetime.now().isoformat(),
                }
                self.session.silent_records.append(record)
                records.append(record)

        return records

    def _check_critical(self, user_message: str, ai_response: str) -> Optional[GuardAlert]:
        """检查严重偏差"""
        if not self.session:
            return None
        combined_raw = user_message + " " + ai_response
        combined = combined_raw.lower()

        critical_keywords = self.CRITICAL_KEYWORDS.get(
            self.session.task_type, []  # type: ignore[attr-defined]
        )  # type: ignore[attr-defined]
        for kw in critical_keywords:
            pos = combined.find(kw.lower())
            if pos >= 0:
                score = self.smart_matcher._contextual_score(combined_raw, kw, pos, 1.0)
                if score < 0.5:
                    continue
                return GuardAlert(
                    level=GuardLevel.INTERRUPT,
                    checklist_item=ChecklistItem(
                        item="严重风险检测", source="system", severity="critical"
                    ),
                    triggered_by="system",
                    trigger_text=kw,
                    suggestion=f"⚠️ 检测到高风险操作关键词「{kw}」，请确认是否继续？",
                )

        return None

    def _check_context_risk(
        self, context: Optional[Dict], user_message: str = ""
    ) -> Optional[GuardAlert]:
        """基于用户操作上下文进行语义风险匹配"""
        if not context:
            return None

        current_file = context.get("current_file", "") or ""
        current_command = context.get("current_command", "") or ""
        git_status = context.get("git_status", "") or ""

        file_lower = current_file.lower()
        op_text = ((current_command or "") + " " + user_message).lower()
        cmd_lower = (current_command or "").lower()
        git_status_text = git_status or ""

        hints: List[str] = []
        score = self._score_prod_file_risk(file_lower, op_text, hints)
        score += self._score_high_risk_command(cmd_lower, hints)
        score += self._score_git_checkout_risk(
            git_status_text, cmd_lower, op_text, hints
        )

        return self._build_context_alert(score, hints)

    def _score_prod_file_risk(
        self, file_lower: str, op_text: str, hints: List[str]
    ) -> int:
        """生产环境文件 + 危险操作评分。"""
        if any(p in file_lower for p in ("prod/", "production/")):
            if any(k in op_text for k in ("rm", "delete", "覆盖", "truncate", "drop")):
                hints.append("生产环境文件危险操作")
                return 4
        return 0

    def _score_high_risk_command(self, cmd_lower: str, hints: List[str]) -> int:
        """高危命令评分。"""
        if any(
            k in cmd_lower
            for k in ("git push --force", "terraform apply", "kubectl delete")
        ):
            hints.append("高危命令执行")
            return 4
        return 0

    def _score_git_checkout_risk(
        self,
        git_status_text: str,
        cmd_lower: str,
        op_text: str,
        hints: List[str],
    ) -> int:
        """未提交修改下切换分支评分。"""
        if (
            "未提交" in git_status_text
            or "modified" in git_status_text.lower()
            or "changes" in git_status_text.lower()
        ):
            if "git checkout" in cmd_lower or "git checkout" in op_text:
                hints.append("未提交修改下切换分支")
                return 2
        return 0

    def _build_context_alert(
        self, score: int, hints: List[str]
    ) -> Optional[GuardAlert]:
        """根据评分构建上下文风险告警。"""
        if not hints:
            return None

        msg = "⚠️ " + "，".join(hints)
        if score >= 4:
            msg += "，请确认。"
            level = GuardLevel.INTERRUPT
            severity = "critical"
            item = "上下文高风险"
        elif score >= 2:
            msg += "，建议检查。"
            level = GuardLevel.HINT
            severity = "high"
            item = "上下文中风险"
        else:
            return None
        msg = msg[:80]

        return GuardAlert(
            level=level,
            checklist_item=ChecklistItem(
                item=item, source="context_guard", severity=severity
            ),
            triggered_by="system",
            trigger_text="; ".join(hints),
            suggestion=msg,
        )

    def smart_check(self, user_message: str, ai_response: str = "") -> List[GuardAlert]:
        """返回所有匹配风险点，按级别排序；用于批量守护和测试。"""
        alerts = []
        first = self.check(user_message, ai_response)
        if first:
            alerts.append(first)
        for record in self.check_silent(user_message, ai_response):
            alerts.append(
                GuardAlert(
                    level=GuardLevel.SILENT,
                    checklist_item=ChecklistItem(
                        item=record["item"],
                        source="system",
                        severity=record["severity"],
                    ),
                    triggered_by="system",
                    trigger_text=record["trigger"],
                    suggestion=f"静默记录：{record['item']}",
                    timestamp=record["timestamp"],
                )
            )
        order = {GuardLevel.INTERRUPT: 0, GuardLevel.HINT: 1, GuardLevel.SILENT: 2}
        return sorted(alerts, key=lambda alert: order[alert.level])

    def _match_three_tier(self, text: str, keywords: List[str]) -> Optional[Dict]:
        """三层级联匹配引擎（精确 → 关键词 → 语义）

        Args:
            text: 待检测文本
            keywords: 关键词/模式列表

        Returns:
            {"layer": int, "type": str, "match": str, "score": float} 或 None
        """
        return self.smart_matcher.match_three_tier(
            text, exact_candidates=keywords, keywords=keywords, semantic_refs=keywords
        )

    def _determine_level(self, item: ChecklistItem, triggered_by: str) -> GuardLevel:
        """确定守护级别"""
        # 严重性映射
        if item.severity == "critical":
            return GuardLevel.INTERRUPT
        elif item.severity == "high":
            return GuardLevel.HINT if triggered_by == "user" else GuardLevel.INTERRUPT
        elif item.severity == "medium":
            # 用户触发 -> HINT，AI 自身风险 -> SILENT（让 AI 自己调整）
            return GuardLevel.HINT if triggered_by == "user" else GuardLevel.SILENT
        else:
            return GuardLevel.SILENT

    def _generate_suggestion(self, item: ChecklistItem) -> str:
        """生成建议文本"""
        if item.detail:
            return f"💡 {item.item}：{item.detail}"
        return f"💡 注意：{item.item}"

    def _record_alert(self, alert: GuardAlert):
        """记录告警"""
        if self.session:
            self.session.triggered_alerts.append(alert)
        # 发射 guard_alert 事件（携带执行上下文）
        try:
            from core.mnemos_bus import publish_event

            payload = {
                "level": alert.level.value,
                "checklist_item": alert.checklist_item.item,
                "triggered_by": alert.triggered_by,
                "trigger_text": alert.trigger_text[:200],
                "session_id": (
                    getattr(self.session, "task_type", "unknown") if self.session else "unknown"
                ),
            }
            if alert.execution_context:
                payload["execution"] = {  # type: ignore[assignment]
                    "task_type": alert.execution_context.task_type,
                    "task_id": alert.execution_context.task_id,
                    "step": alert.execution_context.step,
                    "elapsed": alert.execution_context.elapsed_seconds,
                }
            if alert.metadata:
                payload["metadata"] = dict(alert.metadata)
            publish_event("guard_alert", "aegis", payload)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.warning("Guard alert event publish failed", exc_info=True)

    def _check_thinking_loop(
        self, user_message: str, ai_response: str = "", context: Optional[Dict] = None
    ) -> Optional[GuardAlert]:
        """检测 AI 是否陷入分析瘫痪循环

        基于会话状态进行行为分析，不依赖 checklist 项：
        1. 连续纯分析轮次达到 guard.analysis_loop.max_analysis_turns_without_action 且无行动迹象
        2. 用户说了"修/改/提交"但 AI 回复中无代码块/文件修改
        3. AI 回复中包含循环信号词
        4. 同一文件/工具读取达到 guard.analysis_loop.max_repeated_reads_per_target 但没有行动
        """
        ai_response = ai_response or ""
        if not self._analysis_loop_options.get("enabled", True):
            return None

        (user_message + " " + ai_response).lower()

        # 判断本轮是否为"纯分析轮"（无代码块、无文件修改标记）
        has_code_block = "```" in ai_response
        has_file_edit = any(
            marker in ai_response
            for marker in ["StrReplaceFile", "Edit:", "Write:", "Bash:", "+ ", "- "]
        )
        is_action_turn = has_code_block or has_file_edit or self._has_action_context(context)

        # 更新计数
        if is_action_turn:
            self._analysis_turn_count = 0
            self._tool_read_counts = {}
        else:
            tool_alert = self._check_repeated_tool_reads(context)
            if tool_alert:
                return tool_alert
            # 检测是否包含分析信号词
            analysis_signals = [
                "分析",
                "思考",
                "查看",
                "检查",
                "确认",
                "验证",
                "让我再看看",
                "继续分析",
                "深入研究",
                "仔细看看",
            ]
            if any(s in ai_response for s in analysis_signals):
                self._analysis_turn_count += 1

        max_analysis_turns = int(
            self._analysis_loop_options.get(
                "max_analysis_turns_without_action",
                self.DEFAULT_MAX_ANALYSIS_TURNS_WITHOUT_ACTION,
            )
        )

        # 规则 1：连续纯分析达到配置上限 → HINT
        if self._analysis_turn_count >= max_analysis_turns:
            current_count = self._analysis_turn_count
            self._analysis_turn_count = 0  # 重置避免重复告警
            metadata = self._analysis_loop_metadata(
                threshold_kind="max_analysis_turns_without_action",
                threshold_value=max_analysis_turns,
                current_count=current_count,
            )
            return GuardAlert(
                level=GuardLevel.HINT,
                checklist_item=ChecklistItem(
                    item="思考循环检测：连续多轮纯分析无行动",
                    source="system:thinking-loop-detector",
                    severity="high",
                    detail=(
                        f"threshold_source={metadata['threshold_source']}; "
                        f"threshold_value={metadata['threshold_value']}; "
                        f"current_count={metadata['current_count']}"
                    ),
                ),
                triggered_by="ai",
                trigger_text="连续分析轮次",
                suggestion=(
                    f"💡 已连续 {current_count} 轮分析但未采取行动，达到上限 "
                    f"{max_analysis_turns}。建议：基于已有信息直接开始修复，分析可留给事后复盘。"
                ),
                metadata=metadata,
            )

        # 规则 2：用户要求修复但 AI 还在分析 → HINT
        user_wants_action = any(
            kw in user_message.lower()
            for kw in ["修复", "修", "改", "改一下", "提交", "push", "deploy"]
        )
        if user_wants_action and not is_action_turn:
            return GuardAlert(
                level=GuardLevel.HINT,
                checklist_item=ChecklistItem(
                    item="用户要求行动但 AI 仍在分析",
                    source="system:thinking-loop-detector",
                    severity="critical",
                ),
                triggered_by="user",
                trigger_text=user_message[:50],
                suggestion="💡 用户明确要求修复/修改，请立即停止分析，直接开始行动（代码修改/文件写入/命令执行）。",
            )

        # 规则 3：AI 回复中出现循环信号词 → SILENT（记录即可，不打扰）
        loop_signals = [
            "让我再看看",
            "继续分析",
            "再确认一下",
            "深入研究",
            "再验证",
            "再检查",
            "重新理解",
            "重新分析",
        ]
        if any(s in ai_response for s in loop_signals):
            return GuardAlert(
                level=GuardLevel.SILENT,
                checklist_item=ChecklistItem(
                    item="检测到循环信号词",
                    source="system:thinking-loop-detector",
                    severity="medium",
                ),
                triggered_by="ai",
                trigger_text=next((s for s in loop_signals if s in ai_response), None) or "",
                suggestion='检测到"让我再看看/继续分析"等循环信号，建议评估是否已有足够信息可以行动。',
            )

        return None

    @staticmethod
    def _normalize_tool_events(context: Optional[Dict]) -> List[Dict]:
        """从 context 中提取工具调用事件，兼容不同宿主 Agent 的字段命名。"""
        if not isinstance(context, dict):
            return []

        events: List[Dict] = []
        for key in ("tool_calls", "recent_tool_calls", "tools", "operations"):
            value = context.get(key)
            if isinstance(value, list):
                events.extend(v for v in value if isinstance(v, (dict, str)))  # type: ignore[misc]
            elif isinstance(value, (dict, str)):
                events.append(value)  # type: ignore[arg-type]

        if (
            context.get("current_tool")
            or context.get("current_file")
            or context.get("current_command")
        ):
            events.append(
                {
                    "name": context.get("current_tool") or context.get("tool") or "",
                    "input": {
                        "path": context.get("current_file") or "",
                        "command": context.get("current_command") or "",
                    },
                }
            )
        return events

    @staticmethod
    def _event_name(event) -> str:
        if isinstance(event, str):
            return event
        return str(
            event.get("name")
            or event.get("tool")
            or event.get("tool_name")
            or event.get("recipient_name")
            or event.get("function")
            or ""
        )

    @staticmethod
    def _event_args(event) -> Dict:
        if not isinstance(event, dict):
            return {}
        args = (
            event.get("input")
            or event.get("args")
            or event.get("arguments")
            or event.get("parameters")
            or {}
        )
        return args if isinstance(args, dict) else {}

    @staticmethod
    def _extract_path_from_event(event) -> str:
        if not isinstance(event, dict):
            return ""
        args = InProcessGuard._event_args(event)
        for source in (event, args):
            for key in ("path", "file_path", "filepath", "current_file", "ref_id", "uri"):
                value = source.get(key)
                if value:
                    return str(value)
        command = str(
            args.get("command") or args.get("cmd") or event.get("command") or event.get("cmd") or ""
        )
        match = re.search(
            r'(?:"|\')?([~/A-Za-z0-9_./:-]+\.(?:py|md|json|toml|yaml|yml|txt|sh|ts|tsx|js|jsx|html|css))(?:"|\')?',  # noqa: E501
            command,
        )
        return match.group(1) if match else ""

    @staticmethod
    def _is_read_event(event) -> bool:
        name = InProcessGuard._event_name(event).lower()
        args = InProcessGuard._event_args(event)
        command = str(args.get("command") or args.get("cmd") or "").lower()
        read_markers = ("read", "open", "view", "cat", "sed", "grep", "rg", "find")
        return any(marker in name for marker in read_markers) or command.startswith(read_markers)

    @staticmethod
    def _is_action_event(event) -> bool:
        name = InProcessGuard._event_name(event).lower()
        args = InProcessGuard._event_args(event)
        command = str(args.get("command") or args.get("cmd") or "").lower()
        action_markers = (
            "apply_patch",
            "edit",
            "write",
            "strreplace",
            "create",
            "delete",
            "commit",
        )
        return any(marker in name for marker in action_markers) or any(
            marker in command for marker in action_markers
        )

    def _has_action_context(self, context: Optional[Dict]) -> bool:
        if not isinstance(context, dict):
            return False
        if (
            context.get("action_taken")
            or context.get("edited_files")
            or context.get("modified_files")
        ):
            return True
        return any(self._is_action_event(event) for event in self._normalize_tool_events(context))

    def _check_repeated_tool_reads(self, context: Optional[Dict]) -> Optional[GuardAlert]:
        max_repeated_reads = int(
            self._analysis_loop_options.get(
                "max_repeated_reads_per_target",
                self.DEFAULT_MAX_REPEATED_READS_PER_TARGET,
            )
        )
        for event in self._normalize_tool_events(context):
            if not self._is_read_event(event):
                continue
            name = self._event_name(event) or "unknown-tool"
            path = self._extract_path_from_event(event) or "unknown-target"
            key = f"{name}:{path}"
            self._tool_read_counts[key] = self._tool_read_counts.get(key, 0) + 1
            if self._tool_read_counts[key] >= max_repeated_reads:
                current_count = self._tool_read_counts[key]
                self._tool_read_counts[key] = 0
                metadata = self._analysis_loop_metadata(
                    threshold_kind="max_repeated_reads_per_target",
                    threshold_value=max_repeated_reads,
                    current_count=current_count,
                )
                return GuardAlert(
                    level=GuardLevel.HINT,
                    checklist_item=ChecklistItem(
                        item="思考循环检测：同一文件/工具被重复读取",
                        source="system:thinking-loop-detector",
                        severity="high",
                        detail=(
                            f"threshold_source={metadata['threshold_source']}; "
                            f"threshold_value={metadata['threshold_value']}; "
                            f"current_count={metadata['current_count']}"
                        ),
                    ),
                    triggered_by="ai",
                    trigger_text=key[:120],
                    suggestion=(
                        f"💡 同一文件/工具已重复读取 {current_count} 次，达到上限 "
                        f"{max_repeated_reads}。请停止继续确认，基于已有证据直接修改、验证或向用户汇报阻塞点。"
                    ),
                    metadata=metadata,
                )
        return None

    def get_silent_summary(self) -> str:
        """获取静默记录汇总（任务完成后报告）"""
        if not self.session or not self.session.silent_records:
            return ""

        lines = ["📋 本次任务偏差记录："]
        for i, record in enumerate(self.session.silent_records, 1):
            lines.append(f"  {i}. {record['item']}（触发词：{record['trigger']}）")

        return "\n".join(lines)

    def get_checklist_usage(self) -> List[Dict]:
        """
        获取 checklist 使用情况（用于复盘）

        Returns:
            [{"item": str, "loaded": bool, "used": bool, "triggered": bool, "level": str}, ...]
        """
        if not self.session:
            return []

        usage = []
        triggered_items = {
            a.checklist_item.item: a.level.value for a in self.session.triggered_alerts
        }
        silent_items = {r["item"]: r["severity"] for r in self.session.silent_records}
        hint_items = self.session.hint_used

        for item in self.session.checklist:
            item_name = item.item
            usage.append(
                {
                    "item": item_name,
                    "loaded": True,
                    "used": (
                        item_name in triggered_items
                        or item_name in silent_items
                        or item_name in hint_items
                    ),
                    "triggered": item_name in triggered_items,
                    "level": triggered_items.get(item_name, silent_items.get(item_name, "none")),
                    "severity": item.severity,
                }
            )

        return usage

    def format_hint_for_ai(self, alert: GuardAlert) -> str:
        """
        格式化中等偏差提示，供 AI 自然融入回复

        返回的文本应该能被 AI 在回复中自然引用
        """
        if alert.level != GuardLevel.HINT:
            return ""

        if self.session:
            self.session.hint_used.add(alert.checklist_item.item)

        return (
            f"[Guard Hint] {alert.checklist_item.item}"
            f"{f' - {alert.checklist_item.detail}' if alert.checklist_item.detail else ''}"
        )

    def format_interrupt_message(self, alert: GuardAlert) -> str:
        """格式化严重偏差打断消息"""
        if alert.level != GuardLevel.INTERRUPT:
            return ""

        return (
            f"⚠️ **风险提醒**\n\n"
            f"检测到可能的问题：{alert.checklist_item.item}\n\n"
            f"{alert.suggestion}\n\n"
            f"请确认是否继续当前操作？"
        )


# ========== 便捷函数 ==========


def create_guard(knowledge: LoadedKnowledge) -> InProcessGuard:
    """便捷函数：创建守护"""
    return InProcessGuard(knowledge)
