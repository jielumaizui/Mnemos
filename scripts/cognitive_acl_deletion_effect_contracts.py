"""Shared constants for the COG-043 physical-effect matrix."""

from __future__ import annotations

EFFECT_MATRIX_SCHEMA_VERSION = "mnemos.cognitive_acl_deletion_effect_matrix.v1"
_SESSION_ID = "cog043-effect-matrix-session"
_SUBJECT_SCOPE = f"session:{_SESSION_ID}"
_EVENT_TRACE_ID = "cog043-effect-matrix-event"
_ACTION_TARGET = "cog043-effect-matrix-action"
_REFLECTION_ID = "cog043-effect-matrix-reflection"
_GRAPH_TARGET = "kg://Cog043EffectMatrixPrivateFact"
_KG_RELATION_TARGET = "04-Concepts/cog043-independent-target.md"
_SCORE_SESSION_ID = "cog043-effect-matrix-score"
_MODEL_RUN_ID = "cog043-effect-matrix-model-run"
_SCORING_OBJECT_TYPES = (
    "training_queue",
    "ground_truth",
    "search_session",
    "feedback_event",
    "model",
    "bayesian_state",
    "bayesian_feedback",
    "feedback_prompt",
)
_SCORING_EFFECT_KEYS = {
    "training_queue": "training_samples_deleted",
    "ground_truth": "ground_truth_deleted",
    "search_session": "search_sessions_deleted",
    "feedback_event": "feedback_events_deleted",
    "model": "models_invalidated",
    "bayesian_state": "bayesian_states_invalidated",
    "bayesian_feedback": "bayesian_feedback_deleted",
    "feedback_prompt": "feedback_prompts_deleted",
}
_DOMAIN_FAILURE_MODES = (
    "raw",
    "wiki",
    "embedding_cache",
    "metadata",
    "evidence_refs",
    "persona",
    "reflection",
    "scoring",
    "action_ledger",
    "model_call_ledger",
    "consumer_access_log",
    "agent_source_metadata",
    "cognitive_state",
    "observation",
    "cognitive_graph",
)
_WIKI_CONSUMER_FAILURE_MODES = (
    "knowledge_graph",
    "cognitive_graph",
    "relation_embeddings",
    "wiki_search_index",
    "wiki_metrics",
    "moc_navigation",
)
FAILURE_MODES = tuple(f"domain:{name}" for name in _DOMAIN_FAILURE_MODES) + tuple(
    f"wiki_consumer:{name}" for name in _WIKI_CONSUMER_FAILURE_MODES
)
