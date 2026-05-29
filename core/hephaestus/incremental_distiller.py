# -*- coding: utf-8 -*-
"""
IncrementalDistiller — 增量蒸馏

实时预提取，每 5 轮批量判断，session 结束时结合完整上下文精炼。

设计：
- on_turn()：收集新轮次，每 5 轮触发预提取
- on_session_end()：结合完整上下文精炼草稿
- 草稿持久化：incremental_drafts 表
- LLM 成本预算：每 session 10 次调用
- 冷启动：COLD 阶段不增量蒸馏，WARM 阶段每 10 轮，HOT 阶段每 5 轮
"""

from __future__ import annotations

import json
import logging
logger = logging.getLogger(__name__)
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.config import get_config
from core.hephaestus.distillation_engine import (
    DistillationResult, KnowledgeFragment, HostAgentCaller,
    build_session_text, extract_json,
)



def _get_db_path() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"


# ========== 数据模型 ==========

@dataclass
class IncrementalDraft:
    """增量蒸馏草稿"""
    id: Optional[int] = None
    session_id: str = ""
    turn_start: int = 0
    turn_end: int = 0
    status: str = "draft"  # draft / refined / written / discarded
    fragments_json: str = "[]"
    prejudgment: str = ""
    created_at: str = ""
    refined_at: str = ""

    @property
    def fragments(self) -> List[KnowledgeFragment]:
        if not self.fragments_json:
            return []
        try:
            data = json.loads(self.fragments_json)
            return [KnowledgeFragment(**f) for f in data]
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at incremental_distiller.py", exc_info=True)
            return []

    @fragments.setter
    def fragments(self, value: List[KnowledgeFragment]):
        from dataclasses import asdict
        self.fragments_json = json.dumps([asdict(f) for f in value], ensure_ascii=False)


# ========== 增量蒸馏器 ==========

class IncrementalDistiller:
    """增量蒸馏器

    用法：
        distiller = IncrementalDistiller()
        distiller.on_turn(turn, session_id, session_messages)
        result = distiller.on_session_end(session_id, session_messages)
    """

    DRAFT_TABLE = """
        CREATE TABLE IF NOT EXISTS incremental_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_start INTEGER DEFAULT 0,
            turn_end INTEGER DEFAULT 0,
            status TEXT DEFAULT 'draft',
            fragments_json TEXT DEFAULT '[]',
            prejudgment TEXT DEFAULT '',
            created_at TEXT,
            refined_at TEXT,
            UNIQUE(session_id, turn_start, turn_end)
        )
    """

    def __init__(self, caller: HostAgentCaller = None):
        self._caller = caller or HostAgentCaller()
        self._config = get_config()
        self._mode = self._config.get("scoring.mode", "WARM")
        self._llm_budget = 10  # 每 session 最多 10 次 LLM 调用
        self._llm_calls = 0

        # 会话状态：{session_id: {turns, draft_count, last_draft_turn}}
        self._session_state: Dict[str, Dict] = {}

        # 初始化数据库
        self._init_db()

    def _init_db(self):
        db_path = _get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            conn.execute(self.DRAFT_TABLE)
            conn.commit()

    @property
    def _batch_size(self) -> int:
        """冷启动阶段调整批处理大小"""
        if self._mode == "COLD":
            return 999999  # COLD 阶段不增量蒸馏
        elif self._mode == "WARM":
            return 10
        return 5

    def on_turn(self, turn: Dict, session_id: str,
                session_messages: List[Dict]) -> Optional[IncrementalDraft]:
        """处理新轮次，满足条件时触发预提取

        Args:
            turn: 新消息 {role, content}
            session_id: 会话 ID
            session_messages: 当前会话所有消息（含新轮次）

        Returns:
            预提取草稿（如果触发了批量处理），否则 None
        """
        if self._mode == "COLD":
            return None

        if self._llm_calls >= self._llm_budget:
            logger.debug(f"增量蒸馏 LLM 预算已用尽 ({self._llm_calls}/{self._llm_budget})")
            return None

        state = self._session_state.setdefault(session_id, {
            "turns": [], "draft_count": 0, "last_draft_turn": 0,
        })
        state["turns"].append(turn)
        turn_count = len(state["turns"])
        batch_size = self._batch_size

        # 每 N 轮触发一次
        if turn_count - state["last_draft_turn"] >= batch_size:
            draft = self._batch_extract(
                session_id, session_messages,
                state["last_draft_turn"], turn_count,
            )
            state["last_draft_turn"] = turn_count
            state["draft_count"] += 1
            return draft

        return None

    def on_session_end(self, session_id: str,
                       session_messages: List[Dict]) -> Optional[DistillationResult]:
        """会话结束，结合完整上下文精炼所有草稿

        Args:
            session_id: 会话 ID
            session_messages: 完整会话消息列表

        Returns:
            精炼后的蒸馏结果
        """
        # 加载该 session 的所有草稿
        drafts = self._load_drafts(session_id)

        if not drafts:
            return None

        # 结合完整上下文精炼
        session_text = build_session_text(session_messages)
        refined_fragments = self._refine_with_context(session_text, drafts)

        # 更新草稿状态
        for draft in drafts:
            draft.status = "refined"
            draft.refined_at = datetime.now().isoformat()
            self._update_draft(draft)

        result = DistillationResult(
            session_id=session_id,
            judgment="knowledge",
            judgment_reason="增量蒸馏精炼",
            fragments=refined_fragments,
        )

        # 清理会话状态
        self._session_state.pop(session_id, None)
        return result

    def _batch_extract(self, session_id: str, session_messages: List[Dict],
                       turn_start: int, turn_end: int) -> Optional[IncrementalDraft]:
        """批量预提取"""
        # 获取当前批次的对话片段
        batch_messages = session_messages[turn_start:turn_end]
        if not batch_messages:
            return None

        batch_text = build_session_text(batch_messages, max_chars=4000)

        # 快速价值预判
        from core.hephaestus.distillation_engine import ValuePrejudgment
        vp = ValuePrejudgment()
        verdict, confidence = vp.judge(batch_messages)

        if verdict == ValuePrejudgment.CERTAINLY_NO:
            draft = IncrementalDraft(
                session_id=session_id,
                turn_start=turn_start,
                turn_end=turn_end,
                status="discarded",
                prejudgment=f"{verdict} ({confidence:.2f})",
                created_at=datetime.now().isoformat(),
            )
            self._save_draft(draft)
            return draft

        # LLM 预提取
        if self._llm_calls >= self._llm_budget:
            return None

        prompt = (
            "从以下对话片段中快速提取可能的 knowledge fragments。\n"
            "输出 JSON: {\"fragments\": [{\"form\": \"...\", \"title\": \"...\", "
            "\"core_content\": \"...\", \"confidence\": 0.0-1.0}]}\n"
            "如果不确定，输出空 fragments 数组。\n\n"
            f"对话片段：\n{batch_text}"
        )

        self._llm_calls += 1
        result = self._caller.call(prompt, expect_json=True, max_retries=1, timeout=30)

        fragments = []
        if result and "fragments" in result:
            for f in result["fragments"]:
                try:
                    fragments.append(KnowledgeFragment(
                        form=f.get("form", "unknown"),
                        title=f.get("title", ""),
                        frontmatter={"置信度": f.get("confidence", 0.5)},
                        background="",
                        core_content=f.get("core_content", ""),
                        boundaries={},
                        anti_patterns=[],
                        related_concepts=[],
                    ))
                except Exception:
                    logging.getLogger(__name__).warning(f"Caught unexpected error at incremental_distiller.py", exc_info=True)
                    continue

        draft = IncrementalDraft(
            session_id=session_id,
            turn_start=turn_start,
            turn_end=turn_end,
            status="draft",
            prejudgment=f"{verdict} ({confidence:.2f})",
            created_at=datetime.now().isoformat(),
        )
        draft.fragments = fragments
        self._save_draft(draft)
        return draft

    def _refine_with_context(self, session_text: str,
                             drafts: List[IncrementalDraft]) -> List[KnowledgeFragment]:
        """结合完整上下文精炼草稿"""
        all_fragments = []
        for draft in drafts:
            if draft.status == "discarded":
                continue
            all_fragments.extend(draft.fragments)

        if not all_fragments:
            return []

        # LLM 精炼
        if self._llm_calls < self._llm_budget:
            fragments_summary = "\n".join(
                f"- [{f.form}] {f.title}: {f.core_content[:100]}"
                for f in all_fragments[:20]
            )
            prompt = (
                "以下是增量蒸馏过程中提取的知识片段草稿。"
                "请结合完整对话上下文，精炼这些片段：\n"
                "1. 去除与完整上下文不匹配的片段\n"
                "2. 补充缺失的边界条件\n"
                "3. 合并重复片段\n"
                "4. 调整置信度\n\n"
                f"草稿片段：\n{fragments_summary}\n\n"
                f"完整对话（摘要）：\n{session_text[:3000]}\n\n"
                "输出 JSON: {\"refined_fragments\": [{\"form\": \"...\", \"title\": \"...\", "
                "\"core_content\": \"...\", \"confidence\": 0.0-1.0, \"action\": \"keep|merge|drop\"}]}"
            )
            self._llm_calls += 1
            result = self._caller.call(prompt, expect_json=True, max_retries=1, timeout=30)

            if result and "refined_fragments" in result:
                refined = []
                merged_map: Dict[str, KnowledgeFragment] = {}
                for f in result["refined_fragments"]:
                    action = f.get("action", "keep")
                    if action == "drop":
                        continue
                    frag = KnowledgeFragment(
                        form=f.get("form", "unknown"),
                        title=f.get("title", ""),
                        frontmatter={"置信度": f.get("confidence", 0.5), "提取方式": "incremental_refined"},
                        background="",
                        core_content=f.get("core_content", ""),
                        boundaries={},
                        anti_patterns=[],
                        related_concepts=[],
                    )
                    if action == "merge" and frag.form in merged_map:
                        existing = merged_map[frag.form]
                        existing.core_content += f"\n\n{frag.core_content}"
                    else:
                        refined.append(frag)
                        merged_map[frag.form] = frag
                return refined

        return all_fragments

    # ---- 草稿持久化 ----

    def _save_draft(self, draft: IncrementalDraft):
        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO incremental_drafts
                       (session_id, turn_start, turn_end, status, fragments_json,
                        prejudgment, created_at, refined_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (draft.session_id, draft.turn_start, draft.turn_end,
                     draft.status, draft.fragments_json, draft.prejudgment,
                     draft.created_at, draft.refined_at),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"保存增量蒸馏草稿失败: {e}")

    def _load_drafts(self, session_id: str) -> List[IncrementalDraft]:
        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT id, session_id, turn_start, turn_end, status, "
                    "fragments_json, prejudgment, created_at, refined_at "
                    "FROM incremental_drafts WHERE session_id = ? AND status != 'discarded'",
                    (session_id,),
                )
                drafts = []
                for row in cursor:
                    drafts.append(IncrementalDraft(
                        id=row[0], session_id=row[1], turn_start=row[2],
                        turn_end=row[3], status=row[4], fragments_json=row[5],
                        prejudgment=row[6], created_at=row[7], refined_at=row[8],
                    ))
                return drafts
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at incremental_distiller.py", exc_info=True)
            return []

    def _update_draft(self, draft: IncrementalDraft):
        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                conn.execute(
                    """UPDATE incremental_drafts
                       SET status=?, fragments_json=?, refined_at=?
                       WHERE id=?""",
                    (draft.status, draft.fragments_json, draft.refined_at, draft.id),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"更新增量蒸馏草稿失败: {e}")
