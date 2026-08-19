"""Canonical DecisionTrace helpers for Persona persistence tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.cognitive.decision_trace import MaterialActionAuthorization
from core.persona.delphi import PersonaStore
from core.persona.psyche import (
    PERSONA_VERSION_ACTION,
    PERSONA_VERSION_EXECUTOR,
    PERSONA_VERSION_OWNER,
    SignalStore,
    persona_version_material_action_binding,
)
from tests.cognitive_decision_fixtures import material_action_authorization


def persona_material_action_resolver(database_dir: Path):
    def resolve(request: Mapping[str, str]) -> MaterialActionAuthorization:
        resolved_dir = (
            Path(str(request["expected_state_db"])).parent
            if str(request.get("expected_state_db") or "").strip()
            else Path(database_dir)
        )
        return material_action_authorization(
            resolved_dir,
            action_type=request["action_type"],
            owner=request["owner"],
            executor=request["executor"],
            target_ref=request["target_ref"],
            input_hash=request["input_hash"],
        )

    return resolve


def authorized_persona_store(
    *,
    wiki_dir: Path,
    signal_store: SignalStore,
) -> PersonaStore:
    return PersonaStore(
        wiki_dir=wiki_dir,
        signal_store=signal_store,
        material_action_resolver=persona_material_action_resolver(
            signal_store.db_path.parent
        ),
    )


def save_persona_version_authorized(
    store: SignalStore,
    **kwargs: Any,
) -> int:
    generated_at = str(kwargs.pop("generated_at", "2026-07-17T09:00:00+00:00"))
    binding = persona_version_material_action_binding(
        generated_at=generated_at,
        **kwargs,
    )
    authorization = material_action_authorization(
        store.db_path.parent,
        action_type=PERSONA_VERSION_ACTION,
        owner=PERSONA_VERSION_OWNER,
        executor=PERSONA_VERSION_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    return store.save_persona_version(
        **kwargs,
        generated_at=generated_at,
        material_action=authorization,
    )


def update_blindspot_profile_authorized(
    store: SignalStore,
    blindspot_data: dict[str, Any],
) -> bool:
    authorization = store.prepare_blindspot_material_action(
        blindspot_data,
        source_facts={"blindspot": dict(blindspot_data)},
        evidence_refs=("test-blindspot-admission",),
        created_at="2026-07-17T09:00:00+00:00",
    )
    if authorization is None:
        return store.update_blindspot_profile(blindspot_data)
    return store.update_blindspot_profile(
        blindspot_data,
        material_action=authorization,
    )
