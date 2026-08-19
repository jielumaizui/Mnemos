# -*- coding: utf-8 -*-
"""Build structured retrospective drafts from the three-question flow."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from core.app.forced_retrospective import RecapTask
from core.app.retrospective_models import (
    RetrospectiveActionItem,
    RetrospectiveDraft,
)


class RetrospectiveBuilder:
    """Generate and revise recap drafts from user answers and evidence."""

    ROOT_TYPE_KEYWORDS = {
        "wrong_assumption": ("假设", "目标", "判断错", "理解错", "预期"),
        "execution_gap": ("执行", "漏", "忘", "没跑", "没检查", "未验证"),
        "process_gap": ("流程", "机制", "规则", "检查清单", "gate", "SOP"),
        "collaboration_gap": ("沟通", "协作", "确认", "交接"),
        "tooling_gap": ("工具", "脚本", "自动化", "CI", "测试"),
        "external_factor": ("外部", "不可控", "依赖", "网络", "API"),
    }

    def build_draft(
        self,
        recap_task: RecapTask,
        answers: Dict[str, str],
        evidence_refs: List[str] | None = None,
        recap_id: str = "",
        owner_agent: str = "",
    ) -> RetrospectiveDraft:
        """Build a deterministic structured draft from the three required answers."""
        answers = self.normalize_answers(answers)
        evidence_refs = list(evidence_refs or [])
        goal, actual, delta = self._split_goal_actual(answers.get("goal_actual", ""))
        cause_lesson = answers.get("cause_lesson", "").strip()
        next_answer = answers.get("next_handling", "").strip()
        root_type = self._infer_root_types(cause_lesson, next_answer)
        next_handling = self._infer_next_handling(next_answer)
        action_items = self._build_action_items(
            next_answer,
            next_handling=next_handling,
            owner_agent=owner_agent,
        )
        activation_rules = self._build_activation_rules(recap_task, cause_lesson, next_answer)
        consumption_targets = self._build_consumption_targets(
            next_handling=next_handling,
            activation_rules=activation_rules,
        )

        draft = RetrospectiveDraft(
            recap_id=recap_id,
            task_id=recap_task.task_id,
            title=self._build_title(recap_task.topic, cause_lesson),
            lesson=self._build_lesson(cause_lesson, next_answer),
            goal=goal,
            actual=actual,
            delta=delta,
            root_type=root_type,
            root_cause=cause_lesson,
            next_handling=next_handling,
            no_action_reason=self._no_action_reason(next_answer, next_handling),
            action_items=action_items,
            activation_rules=activation_rules,
            consumption_targets=consumption_targets,
            evidence_refs=evidence_refs,
        )
        draft.missing_fields = self.validate_draft(draft)
        return draft

    @staticmethod
    def normalize_answers(answers: Dict[str, str]) -> Dict[str, str]:
        """Expand a one-sentence recap answer into the three-question contract."""
        normalized = {str(k): str(v).strip() for k, v in (answers or {}).items()}
        freeform = (
            normalized.get("freeform")
            or normalized.get("answer")
            or normalized.get("text")
            or ""
        ).strip()
        if not freeform:
            return normalized

        cause_lesson, next_handling = RetrospectiveBuilder._split_freeform_answer(freeform)
        normalized.setdefault("goal_actual", freeform)
        normalized.setdefault("cause_lesson", cause_lesson or freeform)
        normalized.setdefault("next_handling", next_handling or freeform)
        return normalized

    @staticmethod
    def _split_freeform_answer(answer: str) -> tuple[str, str]:
        text = answer.strip()
        if not text:
            return "", ""
        match = re.search(r"(下次|以后|后续|接下来|之后)", text)
        if not match:
            return text, text
        cause = text[: match.start()].strip(" ，,。；;：:")
        next_handling = text[match.start() :].strip(" ，,。；;：:")
        return cause or text, next_handling or text

    def revise_draft(
        self,
        draft: RetrospectiveDraft,
        user_feedback: str,
    ) -> RetrospectiveDraft:
        """Apply a concise user correction to the lesson and root cause."""
        feedback = user_feedback.strip()
        if not feedback:
            return draft
        draft.root_cause = feedback
        draft.lesson = self._build_lesson(feedback, draft.lesson)
        draft.root_type = self._infer_root_types(feedback, draft.lesson)
        draft.missing_fields = self.validate_draft(draft)
        return draft

    @staticmethod
    def validate_draft(draft: RetrospectiveDraft) -> List[str]:
        """Return missing or contract-violating fields."""
        missing: List[str] = []
        if not draft.goal:
            missing.append("goal")
        if not draft.actual:
            missing.append("actual")
        if not draft.lesson:
            missing.append("lesson")
        if not draft.root_type:
            missing.append("root_type")
        if draft.next_handling == "no_action_needed":
            if not draft.no_action_reason:
                missing.append("no_action_reason")
        elif not draft.action_items:
            missing.append("action_items")
        if not draft.activation_rules:
            missing.append("activation_rules")
        if not draft.consumption_targets:
            missing.append("consumption_targets")
        return missing

    @staticmethod
    def _split_goal_actual(answer: str) -> tuple[str, str, str]:
        text = answer.strip()
        if not text:
            return "", "", ""

        goal = ""
        actual = ""
        delta = ""
        for line in text.splitlines():
            clean = line.strip(" -\t")
            if not clean:
                continue
            if re.search(r"^(目标|当时想|预期)[:：]", clean):
                goal = re.split(r"[:：]", clean, 1)[1].strip()
            elif re.search(r"^(实际|结果|发生)[:：]", clean):
                actual = re.split(r"[:：]", clean, 1)[1].strip()
            elif re.search(r"^(差距|影响)[:：]", clean):
                delta = re.split(r"[:：]", clean, 1)[1].strip()

        if goal or actual:
            return goal or text, actual or text, delta

        separators = ["实际", "结果", "但是", "但", "->", "→"]
        for sep in separators:
            if sep in text:
                left, right = text.split(sep, 1)
                goal = left.strip(" ：:-")
                actual = right.strip(" ：:-")
                break

        if not goal and not actual:
            goal = text
            actual = text
        if not delta and goal != actual:
            delta = f"{goal} / {actual}"
        return goal, actual, delta

    def _infer_root_types(self, *texts: str) -> List[str]:
        merged = " ".join(texts).lower()
        roots = [
            root_type
            for root_type, keywords in self.ROOT_TYPE_KEYWORDS.items()
            if any(keyword.lower() in merged for keyword in keywords)
        ]
        return roots or ["process_gap"]

    @staticmethod
    def _infer_next_handling(answer: str) -> str:
        text = answer.lower()
        if any(token in answer for token in ("无需行动", "不用处理", "不需要行动", "无需处理")):
            return "no_action_needed"
        if any(token in text for token in ("sop", "gate", "checklist")) or any(
            token in answer for token in ("规则", "清单", "必须", "固化", "测试")
        ):
            return "rule_update"
        if any(token in answer for token in ("判断", "假设", "先确认", "先验证")):
            return "judgement_memory"
        return "specific_action"

    @staticmethod
    def _build_action_items(
        answer: str,
        next_handling: str,
        owner_agent: str = "",
    ) -> List[RetrospectiveActionItem]:
        if next_handling == "no_action_needed":
            return []
        action = answer.strip()
        if not action:
            return []
        return [
            RetrospectiveActionItem(
                action_id="action-1",
                action=action,
                owner=owner_agent or "owner_agent",
                metric="下次同类场景执行该动作并确认结果",
                status="open",
            )
        ]

    @staticmethod
    def _build_activation_rules(
        recap_task: RecapTask,
        cause_lesson: str,
        next_answer: str,
    ) -> Dict[str, Any]:
        text = " ".join([recap_task.topic, cause_lesson, next_answer])
        keywords = []
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.-]+|[\u4e00-\u9fff]{2,}", text):
            if token not in keywords:
                keywords.append(token)
            if len(keywords) >= 8:
                break
        rules: Dict[str, Any] = {
            "keywords": keywords,
            "trigger_when": ["task_start", "before_final_answer"],
        }
        if recap_task.current_file:
            rules["current_file_patterns"] = [recap_task.current_file]
        return rules

    @staticmethod
    def _build_consumption_targets(
        next_handling: str,
        activation_rules: Dict[str, Any],
    ) -> List[str]:
        targets = ["wiki_search", "context_aware_search"]
        if activation_rules.get("keywords"):
            targets.extend(["preflight", "guard"])
        if next_handling in ("specific_action", "rule_update"):
            targets.append("follow_up")
        if next_handling in ("judgement_memory", "rule_update"):
            targets.append("persona")
        return targets

    @staticmethod
    def _build_title(topic: str, cause_lesson: str) -> str:
        base = topic.strip() or cause_lesson.strip() or "未命名复盘"
        return f"复盘：{base[:80]}"

    @staticmethod
    def _build_lesson(cause_lesson: str, next_answer: str) -> str:
        if cause_lesson and next_answer:
            return f"{cause_lesson.strip()}；下次：{next_answer.strip()}"
        return (cause_lesson or next_answer).strip()

    @staticmethod
    def _no_action_reason(answer: str, next_handling: str) -> str:
        if next_handling != "no_action_needed":
            return ""
        return answer.strip() or "用户确认无需行动"
