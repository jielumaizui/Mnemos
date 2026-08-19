"""Context-scoped persona strategy helpers and report-only skill suggestions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


CONTEXT_PATTERNS: Dict[str, Dict[str, Any]] = {
    "work": {
        "dir": ("work", "company", "src", "projects"),
        "tags": ("work", "company", "review", "meeting"),
    },
    "personal": {
        "dir": ("personal", "side-project", "hobby"),
        "tags": ("personal", "side-project", "hobby", "experiment"),
    },
    "study": {
        "dir": ("learning", "course", "book", "tutorial", "study"),
        "tags": ("learning", "study", "course", "reading"),
    },
}


@dataclass(frozen=True)
class PersonaContext:
    scope: str
    working_dir: str = ""
    session_tags: Tuple[str, ...] = ()


@dataclass
class ContextualPersonaBuffer:
    signals: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    def add_signal(self, signal: Dict[str, Any]) -> PersonaContext:
        context = detect_persona_context(
            working_dir=str(signal.get("working_dir", "")),
            session_tags=tuple(signal.get("session_tags", signal.get("tags", [])) or ()),
        )
        payload = dict(signal)
        payload["persona_context"] = context.scope
        self.signals.setdefault(context.scope, []).append(payload)
        return context

    def get_signals(self, scope: str) -> List[Dict[str, Any]]:
        return list(self.signals.get(scope, []))


def detect_persona_context(
    working_dir: str = "",
    session_tags: Iterable[str] = (),
) -> PersonaContext:
    path_parts = {part.lower() for part in Path(working_dir or "").parts}
    tags = tuple(str(tag).lower() for tag in session_tags or ())
    tag_set = set(tags)

    for scope, patterns in CONTEXT_PATTERNS.items():
        if path_parts.intersection(patterns["dir"]) or tag_set.intersection(patterns["tags"]):
            return PersonaContext(scope=scope, working_dir=working_dir, session_tags=tags)
    return PersonaContext(scope="default", working_dir=working_dir, session_tags=tags)


class PersonaStrategyBuilder:
    """Build a bounded strategy block from persona and blindspot profiles."""

    STRATEGIES = (
        ("startup_difficulty", 0.6, "用户启动较慢，给出下一步具体动作"),
        ("abstraction", 0.6, "用户偏好抽象原理，先说明 Why 和结构"),
        ("skepticism", 0.6, "用户倾向质疑，主动说明局限与验证方法"),
        ("system_view", 0.6, "用户有系统视角，说明组件关系和整体影响"),
        ("correctness_vs_efficiency", 0.6, "用户重视正确性，提供验证步骤和测试用例"),
        ("innovation_vs_safety", 0.6, "用户偏好创新方案，同时标注风险"),
    )
    BLINDSPOT_STRATEGIES = (
        ("framing_rigidity", 0.6, "可能受问题框架限制，轻量挑战前提"),
        ("option_gap", 0.6, "可能遗漏选项，提供第三种替代方案"),
    )

    def __init__(self, max_strategies: int = 5, token_limit: int = 300, enabled: bool = True):
        self.max_strategies = max_strategies
        self.token_limit = token_limit
        self.enabled = enabled

    def build(
        self,
        preference_profile: Dict[str, Any] | None = None,
        blindspot_profile: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "strategies": [], "prompt": "", "tokens_estimate": 0}

        strategies: List[str] = []
        preference_profile = preference_profile or {}
        blindspot_profile = blindspot_profile or {}
        for key, threshold, text in self.STRATEGIES:
            if float(preference_profile.get(key, 0.0) or 0.0) > threshold:
                strategies.append(text)
        for key, threshold, text in self.BLINDSPOT_STRATEGIES:
            if float(blindspot_profile.get(key, 0.0) or 0.0) > threshold:
                strategies.append(text)

        strategies = strategies[: self.max_strategies]
        lines: List[str] = []
        for strategy in strategies:
            candidate = lines + [f"- {strategy}"]
            if _token_estimate("\n".join(candidate)) > self.token_limit:
                break
            lines = candidate
        prompt = "\n".join(lines)
        return {
            "enabled": True,
            "strategies": [line[2:] for line in lines],
            "prompt": prompt,
            "tokens_estimate": _token_estimate(prompt),
        }


class BehaviorSkillReporter:
    """Report-only behavior-driven skill suggestions."""

    def __init__(self, min_support: float = 0.2):
        self.min_support = min_support

    def suggest(
        self,
        actions: Iterable[Dict[str, Any]],
        current_skills: Iterable[str] = (),
    ) -> Dict[str, Any]:
        action_names = [str(action.get("action", "")).strip() for action in actions]
        action_names = [name for name in action_names if name]
        current = set(current_skills)
        if len(action_names) < 3:
            return {"report_only": True, "suggestions": []}

        pairs = Counter(zip(action_names, action_names[1:]))
        suggestions: List[Dict[str, Any]] = []
        total = max(1, len(action_names) - 1)
        for pattern, count in pairs.most_common():
            support = count / total
            if support < self.min_support:
                continue
            skill_name = "-".join(pattern).replace("_", "-")
            if skill_name in current:
                continue
            suggestions.append(
                {
                    "action": "create",
                    "skill_name": skill_name,
                    "confidence": round(min(1.0, support * 2), 3),
                    "reason": f"高频行为序列 {' -> '.join(pattern)}，支持度 {support:.1%}",
                    "evidence": [f"count={count}", f"total_pairs={total}"],
                }
            )
        return {"report_only": True, "suggestions": suggestions}


def _token_estimate(text: str) -> int:
    return max(0, len(text) // 4)
