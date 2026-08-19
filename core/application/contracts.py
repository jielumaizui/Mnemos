# -*- coding: utf-8 -*-
"""Typed application facade contract for integration adapters."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Protocol

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.evidence.source_authority import SourceAuthorityCatalog


class MnemosServiceFacade(Protocol):
    """Facade contract for integration adapters."""

    def health_check(self) -> Dict:
        """Return a system health report."""

    def agent_runtime_probe(
        self,
        source_agent: str,
        health_check_ids_hash: str,
        sample: Dict[str, Any],
    ) -> Dict:
        """Record one authorized synthetic-safe runtime capability receipt."""

    def agent_health_observed(
        self,
        source_agent: str,
        health_check_ids_hash: str,
    ) -> Dict:
        """Record one authenticated canonical health roundtrip."""

    def self_diagnose(self) -> Dict:
        """Return the full diagnostics report."""

    def configure_wiki(self, vault_path: str) -> Dict:
        """Configure Wiki/Obsidian path."""

    def detect_sources(self) -> Dict:
        """Detect all configured and discoverable data sources."""

    def storage_backend(self) -> Any:
        """Create the configured storage backend."""

    def build_cognitive_state(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "cognitive_state_read",
    ) -> Dict:
        """Build a zero-write canonical cognitive-state read model."""

    def revise_belief(
        self,
        request: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
    ) -> Dict:
        """Append one canonical scoped belief revision."""

    def explain_belief(
        self,
        belief_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> Dict:
        """Read one ACL-filtered canonical belief explanation."""

    def record_decision(
        self,
        trace: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> Dict:
        """Atomically seal a material decision, snapshot, value context and outbox."""

    def apply_outcome(
        self,
        feedback: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> Dict:
        """Atomically persist a typed outcome and its projection outbox."""

    def session_search(
        self,
        query: str = "",
        session_id: str = "",
        uid: str = "",
        limit: int = 10,
        days: int | None = None,
        source: str | None = None,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Search historical session records."""

    def knowledge_ingest(
        self,
        content: str,
        tags: List[str] | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Ingest user-provided knowledge."""

    def wiki_search(
        self,
        query: str,
        limit: int = 5,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> tuple[List[Dict], Dict[str, int]]:
        """Search wiki knowledge and return serialized result items."""

    def wiki_read(
        self,
        page_path: str,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Read a wiki page."""

    def wiki_write(
        self,
        page_path: str,
        content: str,
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """Write a wiki page."""

    def knowledge_source_list(self) -> Dict:
        """Return source distribution statistics for wiki knowledge pages."""

    def session_save(
        self,
        session_id: str,
        messages: List[Dict],
        tags: List[str] | None = None,
        source_agent: str = "unknown",
    ) -> Dict:
        """Save a session through the capture pipeline."""

    def capture_turn(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_id: str = "",
        turn_number: int = 0,
        user_content: str = "",
        assistant_content: str = "",
        timestamp: str = "",
        model: str = "",
        cwd: str = "",
        metadata: Dict | None = None,
        tool_calls: list | None = None,
        tool_results: list | None = None,
        reasoning: str = "",
        attachments: list | None = None,
        raw_event_refs: list | None = None,
        source_files: list | None = None,
        completeness: Dict | None = None,
    ) -> Dict:
        """Queue a captured turn."""

    def capture_session(self, source_agent: str, session_id: str, turns: List[Dict]) -> Dict:
        """Queue a captured session."""

    def end_session(self, source_agent: str, session_id: str) -> Dict:
        """Mark a captured session as ended."""

    def capture_status(
        self, source_agent: str, session_id: str, turn_number: int = -1
    ) -> Dict:
        """Return capture queue status."""

    def knowledge_distill(
        self,
        session_id: str,
        messages: List[Dict],
        write_to_wiki: bool = True,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Queue a session for knowledge distillation."""

    def document_process(
        self,
        file_path: str,
        title: str = "",
        mode: str = "distill",
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Process a document file."""

    def wiki_build(self, dry_run: bool = False) -> Dict:
        """Run catch-up wiki build."""

    def preflight_inject(
        self,
        task_type: str,
        subtype: str = "",
        context_text: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Load KIA preflight knowledge."""

    def check_pending_recaps(self, user_context: Dict | None = None, limit: int = 5) -> Dict:
        """Check pending retrospective reminders."""

    def recap_start(
        self,
        task_id: str = "",
        topic: str = "",
        mode: str = "minimal",
        source_agent: str = "",
        owner_agent: str = "",
        source_agents: List[str] | None = None,
        session_id: str = "",
        context: Dict | None = None,
        project: str = "",
        task_type: str = "",
        subtype: str = "",
    ) -> Dict:
        """Start a structured retrospective session."""

    def recap_submit(
        self,
        recap_id: str,
        answers: Dict,
        confirm_level: str = "draft",
        source_agent: str = "",
    ) -> Dict:
        """Submit retrospective answers and generate a draft."""

    def recap_finalize(
        self,
        recap_id: str,
        write_policy: str = "save_and_index",
        follow_up_at: str = "",
        confirmed_by_user: bool = True,
        source_agent: str = "",
    ) -> Dict:
        """Finalize a retrospective into the Wiki."""

    def recap_skip(
        self,
        recap_id: str = "",
        task_id: str = "",
        skip_reason: str = "",
        user_note: str = "",
        owner_agent: str = "",
        source_agent: str = "",
    ) -> Dict:
        """Record a structured recap skip event."""

    def recap_feedback(
        self,
        recap_id: str,
        feedback_type: str,
        comment: str = "",
        source_agent: str = "",
        supersedes_event_id: str = "",
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Record feedback on a recap."""

    def recap_status(
        self,
        recap_id: str = "",
        task_id: str = "",
        source_agent: str = "",
    ) -> Dict:
        """Return recap status."""

    def recap_claim_owner(
        self,
        recap_id: str,
        owner_agent: str,
        current_session_id: str = "",
    ) -> Dict:
        """Claim recap ownership."""

    def guard_check(
        self,
        user_message: str,
        ai_response: str = "",
        task_type: str = "",
        subtype: str = "",
        context: Dict | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Run KIA in-process guard checks."""

    def retrospective_list(
        self,
        task_type: str | None = None,
        limit: int = 10,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """List available retrospective knowledge files."""

    def persona_summary(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return user persona summary."""

    def persona_behavior_prompt(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return persona-driven behavior prompts."""

    def persona_behavior_metrics(
        self,
        days: int = 30,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return behavior prompt metrics."""

    def record_explicit_profile_evidence(
        self,
        request: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> Dict:
        """Append a Profile v2 fact from one exact explicit-user Raw span."""

    def load_onboarding_prompt(self) -> str:
        """Load host-agent onboarding prompt."""

    def signal_collect(self, sources: List[str] | None = None) -> Dict:
        """Collect persona signals."""

    def persona_update(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Refresh persona profile."""

    def context_aware_search(
        self,
        query: str,
        limit: int = 10,
        working_dir: str = "",
        session_id: str = "",
        project: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
    ) -> Dict:
        """Search knowledge with context and access filtering."""

    def intent_route(self, user_input: str, working_dir: str = "") -> Dict:
        """Route user intent."""

    def intent_correct(self, user_input: str, original_intent: str, corrected_intent: str) -> Dict:
        """Record an intent correction."""

    def blindspot_check(
        self,
        query: str,
        session_id: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Check for knowledge blind spots."""

    def predictive_push(
        self,
        user_input: str,
        working_dir: str = "",
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Return predictive knowledge pushes."""

    def push_feedback(
        self,
        topic: str,
        action: str,
        delivery_event_id: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        """Record a canonical predictive-push reaction or correction."""

    def record_delivery_display(
        self,
        delivery_event_id: str,
        rendered_content_hash: str,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Record a host-bound receipt after the host has rendered a delivery."""

    def freshness_check(
        self,
        entity_name: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Check knowledge freshness."""

    def observation_run(self, full: bool = False, since: str = "") -> Dict:
        """Run the observation engine."""

    def observation_search(
        self,
        dimension: str = "",
        source_type: str = "",
        limit: int = 20,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "observation_read",
    ) -> Dict:
        """Search observations."""

    def build_reflect_tool_result(self, result, route) -> Dict:
        """Build a reflection tool result."""

    def get_reflection_engine(self, use_llm: bool = True):
        """Construct the configured reflection engine."""

    def reflect_on_input(
        self,
        text: str,
        auto_llm: bool = True,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Trigger reflection from user input."""

    def reflect_manually(
        self,
        query: str = "",
        auto_llm: bool = True,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Manually trigger reflection."""

    def reflection_feedback(
        self,
        reflection_id: str,
        feedback_type: str,
        comment: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        """Submit canonical reflection feedback."""

    def reflection_pending(
        self,
        hours_since: float = 24,
        limit: int = 20,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return pending reflection feedback records."""

    def infer_type_from_path(self, page_path: str) -> str:
        """Infer knowledge type from a wiki page path."""

    def scope_slug(self, value: str) -> str:
        """Return a filesystem-friendly scope slug."""

    def scope_page_path(
        self,
        scope: str,
        title: str,
        page_path: str = "",
        scope_name: str = "",
    ) -> str:
        """Build a scoped memory wiki path."""

    def memory_write_project(
        self,
        title: str,
        content: str,
        project: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Write project-scoped memory."""

    def memory_write_framework(
        self,
        title: str,
        content: str,
        framework: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Write framework-scoped memory."""

    def memory_write_global(
        self,
        title: str,
        content: str,
        page_path: str = "",
        frontmatter: Dict | None = None,
        *,
        principal: PrincipalEnvelope,
    ) -> Dict:
        """Write global-scoped memory."""

    def memory_search(
        self,
        query: str,
        scope: str = "all",
        limit: int = 5,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Search scoped memories."""
