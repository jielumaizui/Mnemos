# -*- coding: utf-8 -*-
"""
IntentRouter — 意图路由器

规则匹配（不调用 LLM），4 种意图分类 + 错误路由自愈。

优先级：时间词 > 疑问词 > 动作词 > 默认
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """路由决策"""
    intent: str  # recall / knowledge / task / chat
    confidence: float
    matched_keywords: List[str]
    data_source: str  # memos / wiki / none
    needs_correction: bool = False


class CorrectionStore:
    """错误路由纠正存储 — 三级匹配"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config
            self.DB_PATH = get_config().data_dir / "intent_corrections.db"
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

    def record_correction(self, user_input: str, original_intent: str,
                          corrected_intent: str) -> None:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                INSERT INTO intent_corrections (user_input, original_intent, corrected_intent)
                VALUES (?, ?, ?)
            """, (user_input, original_intent, corrected_intent))

    def lookup(self, user_input: str) -> Optional[str]:
        """三级匹配：L1 精确 → L2 模式 → L3 编辑距离"""
        input_lower = user_input.lower().strip()

        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            # L1: 精确匹配
            cursor = conn.execute("""
                SELECT corrected_intent FROM intent_corrections
                WHERE user_input = ?
                ORDER BY created_at DESC LIMIT 1
            """, (input_lower,))
            row = cursor.fetchone()
            if row:
                return row[0]

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
                    return row[1]

            # L3: 编辑距离（相似度 > 0.7）
            cursor = conn.execute("""
                SELECT user_input, corrected_intent FROM intent_corrections
                ORDER BY created_at DESC LIMIT 100
            """)
            for row in cursor.fetchall():
                sim = SequenceMatcher(None, input_lower, row[0]).ratio()
                if sim > 0.7:
                    return row[1]

        return None


class IntentRouter:
    """意图路由器 — 规则匹配，不调用 LLM"""

    # 优先级从高到低
    INTENT_RULES = [
        {
            "intent": "recall",
            "keywords_time": ["上次", "之前", "刚才", "昨天", "早些时候", "之前那个",
                              "上次那个", "还记得吗", "我们谈过", "做到哪了", "复盘",
                              "总结一下之前", "回到刚才", "接着", "继续"],
            "data_source": "memos",
        },
        {
            "intent": "knowledge",
            "keywords_question": ["是什么", "如何", "怎么", "为什么", "原理", "是什么意思",
                                   "区别", "对比", "有哪些", "哪个好", "怎么用", "怎么理解",
                                   "什么是", "如何实现", "有没有", "能不能"],
            "data_source": "wiki",
        },
        {
            "intent": "task",
            "keywords_action": ["帮我", "创建", "修改", "删除", "运行", "执行", "安装",
                                "配置", "部署", "修复", "重构", "添加", "写", "实现",
                                "检查", "测试", "更新", "上传", "下载", "迁移"],
            "data_source": "none",
        },
    ]

    DEFAULT_INTENT = {
        "intent": "chat",
        "data_source": "none",
    }

    def __init__(self, correction_store: Optional[CorrectionStore] = None):
        self.correction_store = correction_store or CorrectionStore()

    def route(self, user_input: str, context: Optional[Dict] = None) -> RoutingDecision:
        """
        路由用户输入到意图分类。

        优先级：纠正表 > 时间词 > 疑问词 > 动作词 > 默认
        """
        # 先检查纠正表
        corrected = self.correction_store.lookup(user_input)
        if corrected:
            return RoutingDecision(
                intent=corrected,
                confidence=0.95,
                matched_keywords=["correction_store"],
                data_source=self._intent_to_source(corrected),
                needs_correction=False,
            )

        input_lower = user_input.lower()
        matched_keywords = []

        # 按优先级匹配
        for rule in self.INTENT_RULES:
            for key in ["keywords_time", "keywords_question", "keywords_action"]:
                for kw in rule.get(key, []):
                    if kw in input_lower:
                        matched_keywords.append(kw)

                if matched_keywords:
                    confidence = min(0.6 + len(matched_keywords) * 0.1, 0.9)
                    return RoutingDecision(
                        intent=rule["intent"],
                        confidence=confidence,
                        matched_keywords=matched_keywords,
                        data_source=rule["data_source"],
                    )

        # 默认
        return RoutingDecision(
            intent=self.DEFAULT_INTENT["intent"],
            confidence=0.3,
            matched_keywords=[],
            data_source=self.DEFAULT_INTENT["data_source"],
        )

    def correct(self, user_input: str, original_intent: str,
                corrected_intent: str) -> None:
        """记录路由纠正"""
        self.correction_store.record_correction(user_input, original_intent, corrected_intent)

    @staticmethod
    def _intent_to_source(intent: str) -> str:
        mapping = {"recall": "memos", "knowledge": "wiki", "task": "none", "chat": "none"}
        return mapping.get(intent, "none")
