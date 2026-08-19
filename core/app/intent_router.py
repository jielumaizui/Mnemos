# -*- coding: utf-8 -*-
"""
IntentRouter — 意图路由器

规则匹配（不调用 LLM），4 种意图分类 + 错误路由自愈。

优先级：时间词 > 疑问词 > 动作词 > 默认
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional

from core.config import get_config
from core.telemetry.prompt_call_log import (
    ModelCallLedger,
    ModelCallReservation,
    metered_provider_usage,
)
from core.telemetry.provider_request import (
    canonical_chat_input,
    non_redirecting_openai_client,
    safe_provider_error_category,
    utf8_token_upper_bound,
)


@dataclass
class RoutingDecision:
    """路由决策"""

    intent: str
    confidence: float
    matched_keywords: List[str]
    data_source: str
    needs_correction: bool = False
    llm_fallback: bool = False
    route_tools: List[str] = field(default_factory=list)
    fallback_tools: List[str] = field(default_factory=list)
    explanation: str = ""


class CorrectionStore:
    """错误路由纠正存储 — 三级匹配"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            self.DB_PATH = get_config().database_dir / "intent_corrections.db"
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intent_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_input TEXT NOT NULL,
                    original_intent TEXT NOT NULL,
                    corrected_intent TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ic_input
                ON intent_corrections(user_input)
            """)

    def record_correction(
        self, user_input: str, original_intent: str, corrected_intent: str
    ) -> None:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                """
                INSERT INTO intent_corrections (user_input, original_intent, corrected_intent)
                VALUES (?, ?, ?)
            """,
                (user_input, original_intent, corrected_intent),
            )

    def lookup(self, user_input: str) -> Optional[str]:
        """三级匹配：L1 精确 → L2 模式 → L3 编辑距离"""
        input_lower = user_input.lower().strip()

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            # L1: 精确匹配
            cursor = conn.execute(
                """
                SELECT corrected_intent FROM intent_corrections
                WHERE user_input = ?
                ORDER BY created_at DESC LIMIT 1
            """,
                (input_lower,),
            )
            row = cursor.fetchone()
            if row:
                return row[0]  # type: ignore[no-any-return]

            # L2: 模式匹配（关键词交集 > 60%）
            cursor = conn.execute("""
                SELECT user_input, corrected_intent FROM intent_corrections
                ORDER BY created_at DESC LIMIT 100
            """)
            input_words = set(input_lower.split())
            for row in cursor.fetchall():
                past_words = set(row[0].split())
                if not input_words or not past_words:
                    continue
                overlap = len(input_words & past_words) / max(len(input_words | past_words), 1)
                if overlap > 0.6:
                    return row[1]  # type: ignore[no-any-return]

            # L3: 编辑距离（相似度 > 0.7）
            cursor = conn.execute("""
                SELECT user_input, corrected_intent FROM intent_corrections
                ORDER BY created_at DESC LIMIT 100
            """)
            for row in cursor.fetchall():
                sim = SequenceMatcher(None, input_lower, row[0]).ratio()
                if sim > 0.7:
                    return row[1]  # type: ignore[no-any-return]

        return None


class IntentRouter:
    """意图路由器 — 规则匹配，不调用 LLM"""

    INTENT_METADATA = {
        "recap": {
            "data_source": "recap",
            "route_tools": ["check_pending_recaps"],
            "fallback_tools": ["session_search"],
            "explanation": "用户在查复盘、提醒或 follow-up，应先读取复盘提醒队列。",
        },
        "system_status": {
            "data_source": "system",
            "route_tools": ["health_check", "doctor", "status"],
            "fallback_tools": [],
            "explanation": "用户在查系统状态，应走健康检查、doctor 或 status 入口。",
        },
        "persona": {
            "data_source": "persona",
            "route_tools": [
                "persona_summary",
                "persona_behavior_prompt",
                "persona_behavior_metrics",
            ],
            "fallback_tools": ["context_aware_search"],
            "explanation": "用户在查用户画像或行为偏好，应优先读取 persona 工具。",
        },
        "mixed_recall": {
            "data_source": "raw+wiki",
            "route_tools": ["session_search", "context_aware_search"],
            "fallback_tools": ["wiki_search"],
            "explanation": "用户要把历史原话和沉淀知识关联起来，应同时查 raw 和 Mnemos Wiki。",
        },
        "recall": {
            "data_source": "raw",
            "route_tools": ["session_search"],
            "fallback_tools": ["context_aware_search"],
            "explanation": "用户在查原话、证据或历史会话，应优先查 raw/session_search。",
        },
        "ignore_push": {
            "data_source": "none",
            "route_tools": ["push_feedback"],
            "fallback_tools": [],
            "explanation": "用户拒绝推送，应记录或忽略推送，不继续检索。",
        },
        "knowledge": {
            "data_source": "wiki",
            "route_tools": ["context_aware_search", "wiki_search"],
            "fallback_tools": ["session_search"],
            "explanation": "用户在查知识、经验、决策或偏好，应优先查 Mnemos Wiki。",
        },
        "task": {
            "data_source": "none",
            "route_tools": [],
            "fallback_tools": ["preflight_inject", "guard_check"],
            "explanation": "用户要求执行任务，应进入执行流程，必要时再查知识或 raw。",
        },
        "chat": {
            "data_source": "none",
            "route_tools": [],
            "fallback_tools": [],
            "explanation": "用户没有明确检索或执行意图，可以直接回复。",
        },
    }

    # 优先级从高到低；order 越小优先级越高
    INTENT_RULES = [
        {
            "intent": "recap",
            "order": 0,
            "keywords_recap": [
                "复盘提醒",
                "待复盘",
                "待办复盘",
                "recap",
                "follow-up",
                "follow up",
                "提醒",
                "回顾提醒",
            ],
        },
        {
            "intent": "system_status",
            "order": 1,
            "keywords_status": [
                "系统状态",
                "健康检查",
                "health",
                "doctor",
                "status",
                "诊断",
                "运行状态",
                "服务状态",
            ],
        },
        {
            "intent": "persona",
            "order": 2,
            "keywords_persona": [
                "用户画像",
                "我的画像",
                "画像",
                "persona",
                "行为偏好",
                "我的偏好",
                "偏好总结",
            ],
        },
        {
            "intent": "mixed_recall",
            "order": 3,
            "keywords_mixed": [
                "上次怎么解决",
                "之前怎么解决",
                "上次我们怎么解决",
                "之前我们怎么解决",
                "上次怎么处理",
                "之前怎么处理",
                "上次修复",
                "之前修复",
            ],
        },
        {
            "intent": "recall",
            "order": 4,
            "keywords_time": [
                "上次",
                "之前",
                "刚才",
                "昨天",
                "早些时候",
                "之前那个",
                "上次那个",
                "还记得吗",
                "我们谈过",
                "做到哪了",
                "复盘",
                "总结一下之前",
                "回到刚才",
                "接着",
                "继续",
                "原话",
                "证据",
                "聊天记录",
                "对话记录",
                "原始记录",
                "source_event",
            ],
        },
        {
            "intent": "ignore_push",
            "order": 5,
            "keywords_ignore": [
                "不用",
                "不用了",
                "不需要",
                "别推送",
                "忽略",
                "静音",
                "not now",
                "dismiss",
                "关掉推送",
                "取消推送",
                "别烦我",
            ],
        },
        {
            "intent": "knowledge",
            "order": 6,
            "keywords_question": [
                "是什么",
                "如何",
                "怎么",
                "怎么做",
                "怎么办",
                "怎么解决",
                "为什么",
                "原理",
                "是什么意思",
                "区别",
                "对比",
                "有哪些",
                "哪个好",
                "怎么用",
                "怎么理解",
                "什么是",
                "如何实现",
                "如何处理",
                "有没有",
                "能不能",
                "解释一下",
                "讲讲",
                "经验",
                "决策",
                "知识",
                "how do",
                "how to",
                "how can",
                "what is",
                "what are",
                "what's",
                "why",
                "which",
                "explain",
                "difference",
                "compare",
                "best way",
                "is there",
            ],
        },
        {
            "intent": "task",
            "order": 7,
            "keywords_action": [
                "帮我",
                "创建",
                "修改",
                "删除",
                "运行",
                "执行",
                "安装",
                "配置",
                "部署",
                "修复",
                "重构",
                "添加",
                "写",
                "实现",
                "检查",
                "测试",
                "更新",
                "上传",
                "下载",
                "迁移",
                "create",
                "modify",
                "delete",
                "run",
                "execute",
                "install",
                "configure",
                "deploy",
                "fix",
                "refactor",
                "add",
                "write",
                "implement",
                "check",
                "update",
                "upload",
                "download",
                "migrate",
            ],
        },
    ]

    STRONG_TASK_KEYWORDS = {
        "创建",
        "修改",
        "删除",
        "运行",
        "执行",
        "安装",
        "配置",
        "部署",
        "修复",
        "重构",
        "添加",
        "写",
        "实现",
        "检查",
        "测试",
        "更新",
        "上传",
        "下载",
        "迁移",
        "create",
        "modify",
        "delete",
        "run",
        "execute",
        "install",
        "configure",
        "deploy",
        "fix",
        "refactor",
        "add",
        "write",
        "implement",
        "check",
        "update",
        "upload",
        "download",
        "migrate",
    }

    DEFAULT_INTENT = {
        "intent": "chat",
        "data_source": "none",
    }

    # 边界 confidence 区间：命中多个规则或处于此区间时建议人工纠正
    CORRECTION_LOW = 0.5
    CORRECTION_HIGH = 0.7

    def __init__(self, correction_store: Optional[CorrectionStore] = None):
        self.correction_store = correction_store or CorrectionStore()

    def _default_chat_decision(self, confidence: float = 0.0) -> RoutingDecision:
        """构造默认 chat 决策。"""
        return RoutingDecision(
            intent=self.DEFAULT_INTENT["intent"],
            confidence=confidence,
            matched_keywords=[],
            data_source=self.DEFAULT_INTENT["data_source"],
            needs_correction=False,
            route_tools=self._intent_tools(self.DEFAULT_INTENT["intent"]),
            fallback_tools=self._intent_fallback_tools(self.DEFAULT_INTENT["intent"]),
            explanation=self._intent_explanation(self.DEFAULT_INTENT["intent"]),
        )

    def _rule_matches(self, input_lower: str) -> List[Dict]:
        """收集命中关键词的规则列表。"""
        rule_matches = []
        for rule in self.INTENT_RULES:
            matched = []
            for key, keywords in rule.items():
                if not str(key).startswith("keywords_"):
                    continue
                if not isinstance(keywords, list):
                    continue
                for kw in keywords:
                    if kw in input_lower:
                        matched.append(kw)
            if matched:
                rule_matches.append({"rule": rule, "matched": matched})
        return rule_matches

    def _pick_best_rule(self, rule_matches: List[Dict]) -> Optional[Dict]:
        """按优先级与命中数量选择最佳规则。"""
        if not rule_matches:
            return None
        task_match = next((m for m in rule_matches if m["rule"]["intent"] == "task"), None)
        knowledge_match = next(
            (m for m in rule_matches if m["rule"]["intent"] == "knowledge"),
            None,
        )
        if task_match and knowledge_match:
            strong_task_hits = {
                str(kw).lower()
                for kw in task_match["matched"]
                if str(kw).lower() in self.STRONG_TASK_KEYWORDS
            }
            if strong_task_hits:
                task_match["matched"] = [
                    kw for kw in task_match["matched"] if str(kw).lower() in strong_task_hits
                ]
                return task_match
        rule_matches.sort(key=lambda m: (m["rule"]["order"], -len(m["matched"])))
        return rule_matches[0]

    def _try_llm_classify(
        self,
        user_input: str,
        candidates: List[str],
        *,
        allow_llm_fallback: bool = True,
        context: Optional[Dict] = None,
    ) -> Optional[str]:
        """在 LLM fallback 启用时尝试 LLM 分类。"""
        if not allow_llm_fallback:
            return None
        cfg = get_config()
        if not cfg.get("intent_router.llm_fallback_enabled", False):
            return None
        if context:
            return self._llm_classify(user_input, candidates, context=context)
        return self._llm_classify(user_input, candidates)

    @staticmethod
    def _model_call_subject_scope(context: Optional[Dict]) -> tuple[str, str]:
        """Prefer a caller-provided request identity over a fixed router owner."""
        context = context or {}
        for key in ("session_id", "session"):
            value = str(context.get(key) or "").strip()
            if value:
                return "session", value
        project = str(context.get("project") or "").strip()
        if project:
            return "project", project
        for key in ("working_dir", "path"):
            value = str(context.get(key) or "").strip()
            if value:
                return "path", value
        return "source", "intent_router"

    def _build_rule_decision(
        self,
        best: Dict,
        rule_matches: List[Dict],
        context: Dict,
        user_input: str,
        *,
        allow_llm_fallback: bool = True,
    ) -> RoutingDecision:
        """基于命中规则构建决策，必要时走 LLM fallback。"""
        best_rule = best["rule"]
        matched_keywords = best["matched"]
        confidence = min(0.6 + len(matched_keywords) * 0.1, 0.9)

        if best_rule["intent"] == "task" and context.get("current_task"):  # type: ignore[index]
            confidence = min(confidence + 0.1, 0.95)

        needs_correction = len(rule_matches) > 1 or (
            self.CORRECTION_LOW <= confidence <= self.CORRECTION_HIGH
        )
        recent_intent = context.get("recent_intent")
        if recent_intent and recent_intent != best_rule["intent"]:  # type: ignore[index]
            needs_correction = True

        from core.kia.policy import get_shadowed_value

        threshold = float(
            get_shadowed_value(
                "intent_router.llm_fallback_threshold",
                get_config().get("intent_router.llm_fallback_threshold", 0.65),
            )
        )
        if needs_correction and confidence <= threshold:
            candidate_intents = sorted({m["rule"]["intent"] for m in rule_matches})  # type: ignore[index]
            candidate_intents = candidate_intents or list(self.INTENT_METADATA)
            llm_intent = self._try_llm_classify(
                user_input,
                candidate_intents,
                allow_llm_fallback=allow_llm_fallback,
                context=context,
            )
            if llm_intent:
                return RoutingDecision(
                    intent=llm_intent,
                    confidence=0.75,
                    matched_keywords=matched_keywords or ["llm_fallback"],  # type: ignore[arg-type]
                    data_source=self._intent_to_source(llm_intent),
                    needs_correction=False,
                    llm_fallback=True,
                    route_tools=self._intent_tools(llm_intent),
                    fallback_tools=self._intent_fallback_tools(llm_intent),
                    explanation=self._intent_explanation(llm_intent),
                )

        return RoutingDecision(
            intent=best_rule["intent"],  # type: ignore[index]
            confidence=round(confidence, 4),
            matched_keywords=matched_keywords,  # type: ignore[arg-type]
            data_source=self._intent_to_source(best_rule["intent"]),  # type: ignore[index]
            needs_correction=needs_correction,
            route_tools=self._intent_tools(best_rule["intent"]),  # type: ignore[index]
            fallback_tools=self._intent_fallback_tools(best_rule["intent"]),  # type: ignore[index]
            explanation=self._intent_explanation(best_rule["intent"]),  # type: ignore[index]
        )

    def route(
        self,
        user_input: str,
        context: Optional[Dict] = None,
        *,
        allow_llm_fallback: bool = True,
    ) -> RoutingDecision:
        """
        路由用户输入到意图分类。

        优先级：纠正表 > 时间词 > 忽略推送 > 疑问词 > 动作词 > 默认
        """
        if not user_input or not user_input.strip():
            return self._default_chat_decision(0.0)

        corrected = self.correction_store.lookup(user_input)
        if corrected:
            return RoutingDecision(
                intent=corrected,
                confidence=0.95,
                matched_keywords=["correction_store"],
                data_source=self._intent_to_source(corrected),
                needs_correction=False,
                route_tools=self._intent_tools(corrected),
                fallback_tools=self._intent_fallback_tools(corrected),
                explanation=self._intent_explanation(corrected),
            )

        input_lower = user_input.lower().strip()
        context = context or {}
        rule_matches = self._rule_matches(input_lower)
        best = self._pick_best_rule(rule_matches)

        if best is None:
            llm_intent = self._try_llm_classify(
                user_input,
                list(self.INTENT_METADATA),
                allow_llm_fallback=allow_llm_fallback,
                context=context,
            )
            if llm_intent:
                return RoutingDecision(
                    intent=llm_intent,
                    confidence=0.75,
                    matched_keywords=["llm_fallback"],
                    data_source=self._intent_to_source(llm_intent),
                    needs_correction=False,
                    llm_fallback=True,
                    route_tools=self._intent_tools(llm_intent),
                    fallback_tools=self._intent_fallback_tools(llm_intent),
                    explanation=self._intent_explanation(llm_intent),
                )
            return self._default_chat_decision(0.3)

        return self._build_rule_decision(
            best,
            rule_matches,
            context,
            user_input,
            allow_llm_fallback=allow_llm_fallback,
        )

    def correct(self, user_input: str, original_intent: str, corrected_intent: str) -> None:
        """记录路由纠正"""
        self.correction_store.record_correction(user_input, original_intent, corrected_intent)

    def _llm_classify(
        self,
        user_input: str,
        candidates: List[str],
        *,
        context: Optional[Dict] = None,
    ) -> Optional[str]:
        """LLM 兜底分类。失败或返回非法 intent 时返回 None。"""
        cfg = get_config()
        if not cfg.get("intent_router.llm_fallback_enabled", False):
            return None

        try:
            import openai
            from core.llm_config import resolve_llm_api_chain
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError) as e:
            logger = logging.getLogger(__name__)
            logger.debug(
                "意图路由 LLM fallback 不可用: category=%s",
                safe_provider_error_category(e),
            )
            return None
        openai_error_type = getattr(openai, "OpenAIError", RuntimeError)

        chain = resolve_llm_api_chain(cfg)
        configs = chain.all_configs
        if not configs:
            return None

        valid_intents = set(candidates) or set(self.INTENT_METADATA)
        timeout_seconds = self._llm_fallback_timeout_seconds(cfg)
        prompt = (
            "你是意图分类助手。请把下面用户输入分类到唯一意图，输出合法 JSON。\n"
            "可选意图：" + ", ".join(sorted(valid_intents)) + "\n"
            "规则：\n"
            "- recall: 用户想查找之前的对话、上下文、历史记录\n"
            "- mixed_recall: 用户想把历史原话和沉淀知识一起查\n"
            "- knowledge: 用户在询问概念、原理、方案、知识查询\n"
            "- system_status: 用户在查询 Mnemos 系统状态、doctor、health 或 status\n"
            "- persona: 用户在查询用户画像、行为偏好或 persona\n"
            "- recap: 用户在查询复盘、提醒或 follow-up\n"
            "- task: 用户要求执行某个具体操作（创建/修改/运行/部署等）\n"
            "- ignore_push: 用户明确拒绝、忽略、关闭推送\n"
            "- chat: 闲聊、打招呼、无明确意图\n\n"
            f"用户输入：{user_input}\n\n"
            '输出格式：{"intent": "...", "reason": "..."}'
        )

        last_error = ""
        subject_scope = self._model_call_subject_scope(context)
        retry_attempt = 0
        for api_cfg in configs:
            if not api_cfg.configured:
                continue
            active_cfg = api_cfg.active()
            if not active_cfg.configured:
                continue
            current_attempt = retry_attempt
            reservation: ModelCallReservation | None = None
            try:
                client_kwargs = {"api_key": active_cfg.api_key}
                if active_cfg.base_url:
                    client_kwargs["base_url"] = active_cfg.base_url
                messages = [{"role": "user", "content": prompt}]
                provider_input = canonical_chat_input(messages)
                ledger = ModelCallLedger.for_config(cfg)
                run_id = ledger.start_run(
                    f"intent-router:{uuid.uuid4().hex}",
                    subject_scope=subject_scope,
                )
                reservation = ledger.reserve(
                    run_id=run_id,
                    operation="intent_router",
                    provider=active_cfg.provider,
                    model=active_cfg.model,
                    input_text=provider_input,
                    input_tokens=utf8_token_upper_bound(provider_input),
                    output_tokens=120,
                    cache_status="miss",
                    retry_attempt=current_attempt,
                    subject_scopes=(subject_scope,),
                )
                reservation.mark_dispatched()
                retry_attempt += 1
                started = time.perf_counter()
                with non_redirecting_openai_client(
                    openai.OpenAI, **client_kwargs
                ) as client:  # type: ignore[arg-type]
                    response = client.chat.completions.create(
                        model=active_cfg.model,
                        messages=messages,
                        max_tokens=120,
                        temperature=0.2,
                        timeout=timeout_seconds,
                    )
                usage = getattr(response, "usage", None)
                request_id = str(getattr(response, "id", "") or "")
                if reservation is not None:
                    metered_usage = metered_provider_usage(
                        usage,
                        request_id=request_id,
                        output_required=True,
                    )
                    if metered_usage is None:
                        reservation.preserve_incurred(
                            error_code="intent_router_provider_usage_missing"
                        )
                    else:
                        reservation.settle(
                            usage=metered_usage,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                        )
                content = response.choices[0].message.content or ""
                # 简单提取 JSON
                import json

                start = content.find("{")
                end = content.rfind("}")
                if start == -1 or end == -1 or end <= start:
                    api_cfg.report_success(active_cfg)
                    continue
                data = json.loads(content[start : end + 1])
                intent = str(data.get("intent", "")).strip().lower()
                if intent in valid_intents:
                    api_cfg.report_success(active_cfg)
                    return intent
            except (
                openai_error_type,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                IndexError,
                RuntimeError,
            ) as e:
                if reservation is not None:
                    if reservation.dispatched:
                        reservation.preserve_incurred(error_code="intent_router_provider_exception")
                    else:
                        reservation.release(error_code="intent_router_pre_dispatch_exception")
                error_category = safe_provider_error_category(e)
                api_cfg.report_failure(active_cfg, error_category)
                last_error = f"{active_cfg.provider}/{active_cfg.model}: {error_category}"
                logging.getLogger(__name__).debug(
                    "意图路由 LLM fallback 失败: category=%s", error_category
                )
                continue

        if last_error:
            logging.getLogger(__name__).warning("意图路由 LLM fallback 全部失败: %s", last_error)
        return None

    @staticmethod
    def _llm_fallback_timeout_seconds(cfg) -> float:
        """读取并夹紧 LLM fallback 超时，避免热路径被慢供应商拖住。"""
        try:
            timeout = float(cfg.get("intent_router.llm_fallback_timeout_seconds", 2.0))
        except (TypeError, ValueError):
            timeout = 2.0
        return max(0.2, min(timeout, 10.0))

    @staticmethod
    def _intent_to_source(intent: str) -> str:
        meta = IntentRouter.INTENT_METADATA.get(intent, {})
        return str(meta.get("data_source", "none"))

    @staticmethod
    def _intent_tools(intent: str) -> List[str]:
        meta = IntentRouter.INTENT_METADATA.get(intent, {})
        return list(meta.get("route_tools", []))

    @staticmethod
    def _intent_fallback_tools(intent: str) -> List[str]:
        meta = IntentRouter.INTENT_METADATA.get(intent, {})
        return list(meta.get("fallback_tools", []))

    @staticmethod
    def _intent_explanation(intent: str) -> str:
        meta = IntentRouter.INTENT_METADATA.get(intent, {})
        return str(meta.get("explanation", ""))
