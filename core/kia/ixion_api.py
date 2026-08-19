"""Convenience API for the cognitive decision flywheel."""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING, Dict, List

from core.kia.persona_skill_engine import (
    PersonaDrivenSkillEngine,
    PersonaSkillGap,
    SkillPath,
    SkillVerificationTask,
)

if TYPE_CHECKING:
    from core.persona.hamartia import BlindSpotProfile
    from core.persona.pythia import PreferenceProfile


def run_flywheel(
    wiki_base: str | None = None,
    persona: PreferenceProfile | None = None,
    blindspot: BlindSpotProfile | None = None,
    report: bool = False,
    *,
    flywheel_factory: Callable[..., Any],
    persona_report_runner: Callable[..., str],
) -> Dict | str:
    """Run one flywheel cycle, optionally returning the persona report."""
    if report:
        return persona_report_runner(
            persona=persona,
            blindspot=blindspot,
            wiki_base=wiki_base,
        )

    flywheel = flywheel_factory(
        wiki_base=wiki_base,
        persona=persona,
        blindspot=blindspot,
    )
    cycle_result = flywheel.run_cycle()
    if not isinstance(cycle_result, dict):
        raise TypeError("flywheel cycle must return a result mapping")
    return cycle_result


def run_persona_driven_flywheel(
    persona: PreferenceProfile | None = None,
    blindspot: BlindSpotProfile | None = None,
    wiki_base: str | None = None,
    *,
    flywheel_factory: Callable[..., Any],
) -> str:
    """Run a persona-driven cycle and return its report."""
    flywheel = flywheel_factory(
        wiki_base=wiki_base,
        persona=persona,
        blindspot=blindspot,
    )
    results = flywheel.run_cycle()
    report_text = flywheel.generate_cycle_report(results)
    if not isinstance(report_text, str):
        raise TypeError("persona flywheel report must be text")
    return report_text


def get_skill_gaps(persona: PreferenceProfile) -> List[PersonaSkillGap]:
    """Return the skill-gap analysis for one persona."""
    engine = PersonaDrivenSkillEngine(persona)
    return engine.analyze_skill_gaps()


def get_personalized_skill_paths(persona: PreferenceProfile) -> List[SkillPath]:
    """Return personalized learning paths for one persona."""
    engine = PersonaDrivenSkillEngine(persona)
    gaps = engine.analyze_skill_gaps()
    return engine.generate_skill_paths(gaps)


def get_verification_tasks(
    persona: PreferenceProfile,
    blindspot: BlindSpotProfile,
    skills: List[str],
) -> List[SkillVerificationTask]:
    """Return blind-spot verification tasks for selected skills."""
    engine = PersonaDrivenSkillEngine(persona, blindspot)
    return engine.generate_verification_tasks(skills)
