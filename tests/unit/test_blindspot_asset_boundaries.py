"""COG-016 domain-boundary and read-only safety contracts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.access_policy import AccessNarrowing, PrincipalEnvelope


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:blindspot-boundary",
        agent="codex",
        host_kind="codex",
        capability_id="blindspot-boundary",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def test_three_asset_types_have_distinct_identity_and_lifecycle_contracts():
    from core.cognitive.user_model_assets import (
        AssetScope,
        InteractionPreference,
        KnowledgeCoverageGap,
        UserCognitiveBlindspot,
    )

    scope = AssetScope(
        scope_type="project",
        scope_id="mnemos",
        purpose="cognitive_assistance",
    )
    knowledge_gap = KnowledgeCoverageGap.create(
        topic="projection receipts",
        dimension="missing_form",
        description="The vault lacks a decision-form page for projection receipts.",
        evidence_refs=("wiki-scan:sha256:knowledge-gap",),
        scope=scope,
        confidence=0.7,
        expires_at="2026-08-23T00:00:00+00:00",
    )
    cognitive_blindspot = UserCognitiveBlindspot.create(
        blindspot_type="framing",
        description="The current decision options share one premise.",
        evidence_refs=("decision:snapshot-1",),
        user_goal_ref="goal:ship-safe-projection",
        impact="May exclude a safer alternative.",
        scope=scope,
        confidence=0.8,
        expires_at="2026-08-23T00:00:00+00:00",
        invalidation_condition="A later decision snapshot contains independent premises.",
    )
    preference = InteractionPreference.create(
        dimension="interaction_depth",
        value="implementation_ready",
        evidence_refs=("reaction:explicit-1",),
        scope=scope,
        confidence=0.8,
        expires_at="2026-08-23T00:00:00+00:00",
        invalidation_condition="The user explicitly requests a shorter answer.",
    )

    assert knowledge_gap.asset_type == "knowledge_coverage_gap"
    assert cognitive_blindspot.asset_type == "user_cognitive_blindspot"
    assert preference.asset_type == "interaction_preference"
    assert len({knowledge_gap.asset_id, cognitive_blindspot.asset_id, preference.asset_id}) == 3
    assert knowledge_gap.resolution_condition
    assert cognitive_blindspot.user_goal_ref
    assert cognitive_blindspot.impact
    assert preference.invalidation_condition


def test_read_only_blindspot_open_does_not_create_missing_database(tmp_path):
    from core.app.blindspot_discovery import BlindspotDiscovery

    db_path = tmp_path / "missing" / "blindspots.db"
    discovery = BlindspotDiscovery(db_path=str(db_path), initialize=False)

    assert discovery.schema_status()["status"] == "uninitialized"
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_list_and_status_commands_have_zero_database_or_sidecar_delta(tmp_path):
    from core.app.blindspot_discovery import BlindspotDiscovery
    from core.cli.commands.blindspot import cmd_blindspot

    db_path = tmp_path / "blindspots.db"
    BlindspotDiscovery(db_path=str(db_path))
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    with patch("core.cli.commands.blindspot._get_db_path", return_value=db_path):
        assert cmd_blindspot(SimpleNamespace(blindspot_cmd="list", status="")) == 0
        assert cmd_blindspot(SimpleNamespace(blindspot_cmd="status")) == 0

    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime_ns == before_mtime
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_list_and_status_missing_store_do_not_create_parent(tmp_path):
    from core.cli.commands.blindspot import cmd_blindspot

    db_path = tmp_path / "missing" / "blindspots.db"
    with patch("core.cli.commands.blindspot._get_db_path", return_value=db_path):
        assert cmd_blindspot(SimpleNamespace(blindspot_cmd="list", status="")) == 0
        assert cmd_blindspot(SimpleNamespace(blindspot_cmd="status")) == 0

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_cli_manual_resolution_is_fail_closed(tmp_path):
    from core.cli.commands.blindspot import cmd_blindspot

    db_path = tmp_path / "blindspots.db"
    with patch("core.cli.commands.blindspot._get_db_path", return_value=db_path):
        result = cmd_blindspot(
            SimpleNamespace(
                blindspot_cmd="resolve",
                topic="same-theme",
                asset_id="kcg_test",
                resolution_receipt="self-signed",
            )
        )

    assert result == 1
    assert not db_path.exists()


def test_runtime_blindspot_check_emits_only_knowledge_coverage_gap(tmp_path):
    from core.app.blindspot_discovery import BlindspotDiscovery

    discovery = BlindspotDiscovery(db_path=str(tmp_path / "blindspots.db"))

    class _Blindspot:
        type = "framing"
        confidence = 0.95
        description = "The user may be constrained by one frame."

    class _Profile:
        suspected = [_Blindspot()]
        confirmed = []

    with (
        patch(
            "core.app.context_search.ContextAwareSearch.search",
            return_value=[],
        ),
        patch(
            "core.persona.hamartia.BlindSpotProfileManager._load_profile",
            return_value=_Profile(),
        ),
    ):
        result = discovery.check_blind_spot(
            "projection receipts",
            session_id="session-1",
            principal=_principal(),
            narrowing=AccessNarrowing(project="mnemos"),
        )

    assert result.reminder is not None
    assert result.reminder.asset_type == "knowledge_coverage_gap"
    assert result.reminder.topic != "framing_rigidity"


def test_runtime_detector_emits_shadow_hypotheses_not_active_persona_assets():
    from core.persona.hamartia import BlindSpotDetector, BlindSpotProfile
    from core.persona.pythia import PreferenceProfile

    store = MagicMock()
    store.get_recent_session_signals.return_value = []
    detector = BlindSpotDetector(store=store)
    blindspots = detector.detect(
        {
            "session_id": "session-decision-1",
            "user_goal_ref": "goal:choose-safe-runtime",
            "user_message": "Rust async runtime 选择 A 还是 B？",
            "task_type": "architecture",
        },
        [
            {"premise": "single-frame", "keywords": ["A"], "time_horizon": "short"},
            {"premise": "single-frame", "keywords": ["B"], "time_horizon": "short"},
        ],
        PreferenceProfile(),
        BlindSpotProfile(),
    )

    assert blindspots
    for blindspot in blindspots:
        assert not hasattr(blindspot, "asset_id")
        assert not hasattr(blindspot, "status")
        assert blindspot.evidence


def test_wiki_title_similarity_cannot_resolve_without_typed_projection_evidence(tmp_path):
    from core.app.blindspot_discovery import BlindspotDiscovery
    from core.cognitive.user_model_assets import AssetScope

    db_path = tmp_path / "blindspots.db"
    discovery = BlindspotDiscovery(db_path=str(db_path), wiki_base=str(tmp_path))
    with patch("core.app.context_search.ContextAwareSearch.search", return_value=[]):
        result = discovery.check_blind_spot(
            "rustasync",
            session_id="session-1",
            principal=_principal(),
            narrowing=AccessNarrowing(project="mnemos"),
        )
    assert result.reminder is not None

    page = tmp_path / "Rust-async-runtime.md"
    page.write_text("# Rust async runtime\n", encoding="utf-8")
    content_hash = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()

    assert discovery.resolve_by_wiki_page(str(page)) == 0
    assert (
        discovery.mark_resolved(
            result.reminder.topic,
            asset_id=result.reminder.asset_id,
            resolution_evidence=("projection-receipt:not-a-coverage-proof",),
        )
        is False
    )
    assert (
        discovery.resolve_by_wiki_page(
            str(page),
            canonical_revision_id="wiki-revision-1",
            projection_receipt_id="wiki-projection-receipt-1",
            content_hash=content_hash,
        )
        == 0
    )
    scope_key = AssetScope(
        scope_type=result.reminder.scope_type,
        scope_id=result.reminder.scope_id,
        purpose=result.reminder.purpose,
        principal_id=result.reminder.principal_id,
    ).key
    assert (
        discovery.resolve_by_wiki_page(
            str(page),
            canonical_revision_id="wiki-revision-1",
            projection_receipt_id="wiki-projection-receipt-1",
            content_hash=content_hash,
            coverage_evidence=(
                {
                    "receipt_id": "self-signed-projection",
                    "asset_id": result.reminder.asset_id,
                    "gap_revision_id": result.reminder.revision_id,
                    "scope_key": scope_key,
                    "verifier_id": "wiki_projection",
                    "verification_method": "title-match",
                    "content_hash": content_hash,
                    "verified_at": "2026-07-23T00:00:00+00:00",
                    "outcome": "covered",
                },
            ),
        )
        == 0
    )
    assert (
        discovery.resolve_by_wiki_page(
            str(page),
            canonical_revision_id="wiki-revision-1",
            projection_receipt_id="wiki-projection-receipt-1",
            content_hash=content_hash,
            coverage_evidence=(
                {
                    "receipt_id": "coverage-recheck-1",
                    "asset_id": result.reminder.asset_id,
                    "gap_revision_id": result.reminder.revision_id,
                    "scope_key": scope_key,
                    "verifier_id": "knowledge-coverage-auditor-v1",
                    "verification_method": "authorized-context-requery",
                    "content_hash": content_hash,
                    "verified_at": "2026-07-23T00:00:00+00:00",
                    "outcome": "covered",
                },
            ),
        )
        == 1
    )


def test_distilled_page_exposes_form_consumed_by_knowledge_gap_detector(tmp_path):
    from core.kia.hygieia import KnowledgeImmuneSystem
    from core.hephaestus.distillation_models import KnowledgeFragment
    from core.hephaestus.distillation_wiki_page import generate_wiki_page

    fragment = KnowledgeFragment(
        form="decision",
        title="Projection receipt ownership decision",
        frontmatter={"领域": "认知系统", "摘要": "投影回执的唯一所有权决策。"},
        background="The lifecycle needs one owner.",
        core_content="## Decision\nUse the canonical projection lifecycle and reciprocal receipts.",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        ai_expansion="",
        relations=[],
        self_check_severity="",
    )

    rendered = generate_wiki_page(fragment, session_id="session-1", source="codex")

    assert "知识形态: decision" in rendered
    page = tmp_path / "decision.md"
    page.write_text(rendered, encoding="utf-8")
    parsed = KnowledgeImmuneSystem(wiki_base=str(tmp_path))._parse_page_metadata(page)
    assert parsed is not None
    assert parsed[2] == {"decision"}
