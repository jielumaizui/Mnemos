# -*- coding: utf-8 -*-
"""LLM semantic value-judgment stage for distillation."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Callable, Tuple

from core.hephaestus.distill_backend import DistillBackend, LLMBackend
from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
from core.hephaestus.prompt_builder import DistillTask, PromptBuilder, Session, TokenBudget


class LLMValueJudge:
    """第3层：LLM 语义判断 — 宿主 Agent 调用"""

    def __init__(
        self,
        backend: DistillBackend | None = None,
        caller: HttpApiHostAgentCaller | None = None,
        prompt_budget_getter: Callable[[], TokenBudget] | None = None,
    ):
        if backend is None and caller is not None:
            backend = LLMBackend(caller)
        if backend is None:
            raise RuntimeError("distill backend is required")
        self._backend = backend
        self._prompt_budget_getter = prompt_budget_getter

    def judge(self, session_text: str, session_id: str = "") -> Tuple[str, str, float]:
        """LLM 价值判断，返回 (判断, 理由, 置信度)。"""
        if self._prompt_budget_getter is None:
            raise RuntimeError("prompt_budget_getter is required")
        session = Session(
            id=session_id or "",
            messages=[{"role": "user", "content": session_text}],
            agent_name="",
        )
        task = DistillTask(
            task_type="value_judge",
            session=session,
            session_type="general",
            budget_config=self._prompt_budget_getter(),
            preformatted=True,
        )
        prompt = PromptBuilder().build(task)
        caller = getattr(self._backend, "caller", None)
        operation_context = getattr(caller, "model_call_context", None)
        context = operation_context("distill_judge") if callable(operation_context) else nullcontext()
        with context:
            response = self._backend.call(prompt, expect_json=True)
            result = response.require_mapping()

        judgment = result.get("judgment", "skip")
        reason = result.get("reason", "")
        confidence = 0.5
        if judgment == "knowledge":
            confidence = 0.7
        elif judgment == "skill":
            confidence = 0.6

        return judgment, reason, confidence
