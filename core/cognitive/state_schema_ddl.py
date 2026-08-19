"""Immutable SQLite DDL contracts for the cognitive-state ledger."""

from __future__ import annotations

from core.cognitive.state_contract import sha256_json


STATE_SCHEMA_VERSION = "mnemos.cognitive_state_store.v5"
LEGACY_CANONICAL_V4_SCHEMA_VERSION = "mnemos.cognitive_state_store.v4"
LEGACY_CANONICAL_V4_DDL_HASH = (
    "sha256:cf1e076f4e6c7957f243af0f7e93b29725aecb5e672783009dde25d216031cb8"
)
LEGACY_CANONICAL_V3_SCHEMA_VERSION = "mnemos.cognitive_state_store.v3"
LEGACY_CANONICAL_V3_DDL_HASH = (
    "sha256:05ce0467ff495ed34781f5204962ac3f75ad507e0291f9312ede99f3e68a633e"
)
LEGACY_CANONICAL_V2_SCHEMA_VERSION = "mnemos.cognitive_state_store.v2"
LEGACY_CANONICAL_V2_DDL_HASH = (
    "sha256:bb0499a33bd1de82e89fd5ce9d39e787f08d671acd6b5c71d9e1b688f986c7cc"
)
LEGACY_CANONICAL_V1_SCHEMA_VERSION = "mnemos.cognitive_state_store.v1"
LEGACY_CANONICAL_V1_DDL_HASH = (
    "sha256:ad3b9691b4ddc92a1807099d9fbaf78025f02683ef225276f52075a7ae3aa9c1"
)
RUNTIME_LEDGER_SCHEMA_VERSION = "mnemos.runtime_producer_consumer.v4"
SCHEMA_COMPONENT = "cognitive_state_store"
REGISTRY_TABLE = "mnemos_schema_registry"
DECISION_TRACE_ENFORCEMENT_COMPONENT = "decision_trace_enforcement"
DECISION_TRACE_ENFORCEMENT_VERSION = "mnemos.decision_trace_enforcement.v1"
DECISION_TRACE_ENFORCEMENT_HASH = sha256_json(
    {
        "schema_version": DECISION_TRACE_ENFORCEMENT_VERSION,
        "mode": "strict",
        "material_action_command": "execute_material_action",
        "historical_state": "historical_incomplete",
    }
)
PREDICTION_ENFORCEMENT_COMPONENT = "prediction_enforcement"
PREDICTION_ENFORCEMENT_VERSION = "mnemos.prediction_enforcement.v1"
PREDICTION_ENFORCEMENT_HASH = sha256_json(
    {
        "schema_version": PREDICTION_ENFORCEMENT_VERSION,
        "mode": "strict",
        "prediction_schema": "mnemos.prediction_record.v1",
        "terminal_states": ["censored", "confounded", "measured", "unknown"],
        "historical_state": "historical_unverifiable_prediction",
    }
)

CANONICAL_TABLES = (
    "runtime_flow_registry",
    "runtime_flow_events",
    "runtime_flow_receipts",
    "cognitive_data_events",
    "cognitive_data_consumptions",
    "cognitive_data_consumer_heads",
    "cognitive_data_reconciliations",
    "cognitive_state_revisions",
    "cognitive_state_heads",
    "cognitive_state_outbox",
    "cognitive_feedback_command_attempts",
    "cognitive_state_effect_receipts",
    "cognitive_state_migration_quarantine",
    REGISTRY_TABLE,
)

LEGACY_CANONICAL_TABLES = tuple(
    table for table in CANONICAL_TABLES if table != "cognitive_feedback_command_attempts"
)


FEEDBACK_COMMAND_ATTEMPT_DDL = """
CREATE TABLE cognitive_feedback_command_attempts (
    attempt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    target_id TEXT NOT NULL CHECK(length(trim(target_id)) > 0),
    command_type TEXT NOT NULL CHECK(length(trim(command_type)) > 0),
    command_payload_hash TEXT NOT NULL CHECK(length(trim(command_payload_hash)) > 0),
    attribution_payload_hash TEXT NOT NULL CHECK(
        length(trim(attribution_payload_hash)) > 0
    ),
    proof_hash TEXT NOT NULL CHECK(length(trim(proof_hash)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(command_id) REFERENCES cognitive_state_outbox(command_id)
        ON DELETE RESTRICT
);
CREATE INDEX idx_cognitive_feedback_attempts_command
    ON cognitive_feedback_command_attempts(command_id);
CREATE TRIGGER cognitive_feedback_command_attempts_no_update
BEFORE UPDATE ON cognitive_feedback_command_attempts BEGIN
    SELECT RAISE(ABORT, 'cognitive_feedback_command_attempts are immutable');
END;
CREATE TRIGGER cognitive_feedback_command_attempts_no_delete
BEFORE DELETE ON cognitive_feedback_command_attempts BEGIN
    SELECT RAISE(ABORT, 'cognitive_feedback_command_attempts are immutable');
END;
"""

CANONICAL_DDL = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE runtime_flow_registry (
    flow_id TEXT PRIMARY KEY,
    data_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    producer_refs TEXT NOT NULL CHECK(json_valid(producer_refs)),
    consumer_refs TEXT NOT NULL CHECK(json_valid(consumer_refs)),
    pending_budget INTEGER NOT NULL DEFAULT 0 CHECK(pending_budget >= 0),
    dead_letter_budget INTEGER NOT NULL DEFAULT 0 CHECK(dead_letter_budget >= 0),
    max_lag_seconds INTEGER NOT NULL CHECK(max_lag_seconds >= 0),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0, 1)),
    min_observations INTEGER NOT NULL DEFAULT 1 CHECK(min_observations >= 0),
    observation_mode TEXT NOT NULL DEFAULT 'continuous'
        CHECK(observation_mode IN ('continuous', 'on_event', 'not_applicable')),
    not_applicable_reason TEXT NOT NULL DEFAULT '',
    freshness_required INTEGER NOT NULL DEFAULT 1 CHECK(freshness_required IN (0, 1)),
    receipt_grace_seconds INTEGER NOT NULL DEFAULT 0 CHECK(receipt_grace_seconds >= 0)
);

CREATE TABLE runtime_flow_events (
    event_id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction = 'produced'),
    topic TEXT NOT NULL,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL CHECK(json_valid(metadata)),
    generation_id TEXT NOT NULL,
    intended_consumers TEXT NOT NULL CHECK(
        json_valid(intended_consumers)
        AND json_type(intended_consumers)='array'
    ),
    idempotency_key TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(flow_id) REFERENCES runtime_flow_registry(flow_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX idx_runtime_flow_events_idempotency
    ON runtime_flow_events(idempotency_key) WHERE idempotency_key != '';
CREATE INDEX idx_runtime_flow_events_flow_direction
    ON runtime_flow_events(flow_id, direction);
CREATE INDEX idx_runtime_flow_events_created_at ON runtime_flow_events(created_at);
CREATE INDEX idx_runtime_flow_events_item
    ON runtime_flow_events(flow_id, item_id, direction);

CREATE TABLE runtime_flow_receipts (
    receipt_id TEXT PRIMARY KEY,
    production_event_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('consumed', 'dead_letter', 'skipped', 'in_progress')),
    item_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL CHECK(json_valid(metadata)),
    FOREIGN KEY(flow_id) REFERENCES runtime_flow_registry(flow_id) ON DELETE RESTRICT
);
CREATE INDEX idx_runtime_flow_receipts_event
    ON runtime_flow_receipts(production_event_id, consumer_id, created_at);
CREATE INDEX idx_runtime_flow_receipts_flow
    ON runtime_flow_receipts(flow_id, status, created_at);

CREATE TABLE cognitive_data_events (
    event_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL DEFAULT '',
    asset_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    canonical_subject TEXT NOT NULL,
    data_type TEXT NOT NULL,
    producer TEXT NOT NULL,
    intended_consumers TEXT NOT NULL CHECK(
        json_valid(intended_consumers)
        AND json_type(intended_consumers)='array'
        AND (
            json_array_length(intended_consumers) > 0
            OR data_type='decision_trace'
        )
    ),
    privacy_level TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    evidence_refs TEXT NOT NULL CHECK(
        json_valid(evidence_refs)
        AND json_type(evidence_refs)='array'
        AND json_array_length(evidence_refs) > 0
    ),
    dedupe_key TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'produced'
        CHECK(lifecycle_status IN (
            'produced', 'normalized', 'deduped', 'rejected',
            'expired', 'superseded', 'dead_letter'
        )),
    retention_policy TEXT NOT NULL,
    metadata TEXT NOT NULL CHECK(json_valid(metadata) AND json_type(metadata)='object'),
    created_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX idx_cognitive_data_events_dedupe ON cognitive_data_events(dedupe_key);
CREATE INDEX idx_cognitive_data_events_subject
    ON cognitive_data_events(canonical_subject, data_type);

CREATE TABLE cognitive_data_consumptions (
    consumption_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN (
        'committed', 'failed_terminal', 'intentional_skip', 'rejected',
        'revoked', 'dead_letter', 'expired', 'superseded'
    )),
    target_effect_id TEXT NOT NULL DEFAULT '',
    before_hash TEXT NOT NULL DEFAULT '',
    after_hash TEXT NOT NULL DEFAULT '',
    effect_evidence_refs TEXT NOT NULL DEFAULT '[]' CHECK(
        json_valid(effect_evidence_refs) AND json_type(effect_evidence_refs)='array'
    ),
    action_changed INTEGER NOT NULL CHECK(
        action_changed = CASE
            WHEN target_effect_id != ''
             AND before_hash != ''
             AND after_hash != ''
             AND before_hash != after_hash
             AND json_array_length(effect_evidence_refs) > 0
            THEN 1 ELSE 0 END
    ),
    metadata TEXT NOT NULL CHECK(json_valid(metadata) AND json_type(metadata)='object'),
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
    supersedes_consumption_id TEXT,
    correction_of_consumption_id TEXT,
    receipt_state TEXT NOT NULL DEFAULT 'active'
        CHECK(receipt_state IN ('active', 'historical_incomplete', 'quarantined')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES cognitive_data_events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_consumption_id)
        REFERENCES cognitive_data_consumptions(consumption_id) ON DELETE RESTRICT,
    FOREIGN KEY(correction_of_consumption_id)
        REFERENCES cognitive_data_consumptions(consumption_id) ON DELETE RESTRICT,
    CHECK(length(trim(consumer_id)) > 0),
    CHECK(supersedes_consumption_id IS NULL OR supersedes_consumption_id != consumption_id),
    CHECK(correction_of_consumption_id IS NULL OR correction_of_consumption_id != consumption_id),
    UNIQUE(event_id, consumer_id, consumption_id)
);
CREATE INDEX idx_cognitive_data_consumptions_event
    ON cognitive_data_consumptions(event_id, consumer_id, created_at);

CREATE TABLE cognitive_data_consumer_heads (
    event_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    consumption_id TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(event_id, consumer_id),
    FOREIGN KEY(event_id) REFERENCES cognitive_data_events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY(event_id, consumer_id, consumption_id)
        REFERENCES cognitive_data_consumptions(event_id, consumer_id, consumption_id)
        ON DELETE RESTRICT
);

CREATE TABLE cognitive_data_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    related_event_id TEXT NOT NULL,
    relation_type TEXT NOT NULL CHECK(relation_type IN ('duplicate', 'derived', 'reinforcement')),
    dedupe_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_revision_refs TEXT NOT NULL CHECK(json_valid(source_revision_refs)),
    proof_hash TEXT NOT NULL,
    proof_status TEXT NOT NULL CHECK(
        proof_status IN ('verified', 'historical_heuristic', 'quarantined')
    ),
    metadata TEXT NOT NULL CHECK(json_valid(metadata)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES cognitive_data_events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY(related_event_id) REFERENCES cognitive_data_events(event_id) ON DELETE RESTRICT,
    CHECK(event_id != related_event_id),
    UNIQUE(event_id, related_event_id, relation_type)
);
CREATE INDEX idx_cognitive_data_reconciliations_relation
    ON cognitive_data_reconciliations(relation_type, proof_status);

CREATE TABLE cognitive_state_revisions (
    revision_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL CHECK(object_type IN (
        'cognition_episode', 'belief_revision', 'cognitive_state_snapshot',
        'decision_trace', 'prediction_record', 'value_context',
        'calibration_record', 'user_reaction_event', 'outcome_measurement',
        'cognitive_update_receipt', 'feedback_attribution_record',
        'training_admission_record', 'training_run_record'
    )),
    object_id TEXT NOT NULL CHECK(length(trim(object_id)) > 0),
    schema_version TEXT NOT NULL CHECK(length(trim(schema_version)) > 0),
    revision_no INTEGER NOT NULL CHECK(revision_no > 0),
    source_event_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL CHECK(length(trim(source_revision_id)) > 0),
    source_content_hash TEXT NOT NULL CHECK(length(trim(source_content_hash)) > 0),
    scope_type TEXT NOT NULL CHECK(length(trim(scope_type)) > 0),
    scope_id TEXT NOT NULL CHECK(length(trim(scope_id)) > 0),
    evidence_refs TEXT NOT NULL CHECK(
        json_valid(evidence_refs)
        AND json_type(evidence_refs)='array'
        AND json_array_length(evidence_refs) > 0
    ),
    evidence_hash TEXT NOT NULL CHECK(length(trim(evidence_hash)) > 0),
    payload_json TEXT NOT NULL CHECK(
        json_valid(payload_json) AND json_type(payload_json)='object'
    ),
    payload_hash TEXT NOT NULL CHECK(length(trim(payload_hash)) > 0),
    supersedes_revision_id TEXT,
    correction_of_revision_id TEXT,
    admission_state TEXT NOT NULL CHECK(
        admission_state IN ('active', 'historical_candidate', 'quarantined')
    ),
    redaction_policy TEXT NOT NULL,
    redaction_counts TEXT NOT NULL CHECK(json_valid(redaction_counts)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_event_id) REFERENCES cognitive_data_events(event_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(supersedes_revision_id)
        REFERENCES cognitive_state_revisions(revision_id) ON DELETE RESTRICT,
    FOREIGN KEY(correction_of_revision_id)
        REFERENCES cognitive_state_revisions(revision_id) ON DELETE RESTRICT,
    CHECK(supersedes_revision_id IS NULL OR supersedes_revision_id != revision_id),
    CHECK(correction_of_revision_id IS NULL OR correction_of_revision_id != revision_id),
    UNIQUE(object_type, object_id, revision_no),
    UNIQUE(object_type, object_id, revision_id)
);
CREATE INDEX idx_cognitive_state_revisions_source
    ON cognitive_state_revisions(source_event_id, source_revision_id);
CREATE INDEX idx_cognitive_state_revisions_scope
    ON cognitive_state_revisions(scope_type, scope_id, object_type);

CREATE TABLE cognitive_state_heads (
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    revision_id TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(object_type, object_id),
    FOREIGN KEY(object_type, object_id, revision_id)
        REFERENCES cognitive_state_revisions(object_type, object_id, revision_id)
        ON DELETE RESTRICT
);

CREATE TABLE cognitive_state_outbox (
    command_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL CHECK(length(trim(consumer_id)) > 0),
    command_type TEXT NOT NULL CHECK(length(trim(command_type)) > 0),
    payload_json TEXT NOT NULL CHECK(
        json_valid(payload_json) AND json_type(payload_json)='object'
    ),
    payload_hash TEXT NOT NULL CHECK(length(trim(payload_hash)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(event_id) REFERENCES cognitive_data_events(event_id) ON DELETE RESTRICT,
    UNIQUE(revision_id, consumer_id, command_type)
);
CREATE INDEX idx_cognitive_state_outbox_consumer
    ON cognitive_state_outbox(consumer_id, created_at);
CREATE INDEX idx_cognitive_state_outbox_command_type
    ON cognitive_state_outbox(command_type, revision_id);
CREATE INDEX idx_cognitive_state_outbox_pending_order
    ON cognitive_state_outbox(created_at, command_id);

CREATE TABLE cognitive_state_effect_receipts (
    receipt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    revision_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    consumption_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN (
        'committed', 'failed_terminal', 'intentional_skip', 'rejected',
        'revoked', 'dead_letter'
    )),
    target_effect_id TEXT NOT NULL CHECK(length(trim(target_effect_id)) > 0),
    before_hash TEXT NOT NULL DEFAULT '',
    after_hash TEXT NOT NULL DEFAULT '',
    evidence_refs TEXT NOT NULL CHECK(
        json_valid(evidence_refs)
        AND json_type(evidence_refs)='array'
        AND json_array_length(evidence_refs) > 0
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY(command_id) REFERENCES cognitive_state_outbox(command_id) ON DELETE RESTRICT,
    FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(event_id) REFERENCES cognitive_data_events(event_id) ON DELETE RESTRICT,
    FOREIGN KEY(consumption_id) REFERENCES cognitive_data_consumptions(consumption_id)
        ON DELETE RESTRICT,
    CHECK(status != 'committed' OR (before_hash != '' AND after_hash != ''))
);

{FEEDBACK_COMMAND_ATTEMPT_DDL}

CREATE TABLE cognitive_state_migration_quarantine (
    quarantine_id TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_key TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    field_manifest TEXT NOT NULL CHECK(json_valid(field_manifest)),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_table, source_key, reason_code)
);

CREATE TABLE {REGISTRY_TABLE} (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TRIGGER cognitive_data_events_no_update
BEFORE UPDATE ON cognitive_data_events BEGIN
    SELECT RAISE(ABORT, 'cognitive_data_events are immutable');
END;
CREATE TRIGGER cognitive_data_events_no_delete
BEFORE DELETE ON cognitive_data_events BEGIN
    SELECT RAISE(ABORT, 'cognitive_data_events are immutable');
END;
CREATE TRIGGER cognitive_data_consumptions_no_update
BEFORE UPDATE ON cognitive_data_consumptions BEGIN
    SELECT RAISE(ABORT, 'cognitive_data_consumptions are immutable');
END;
CREATE TRIGGER cognitive_data_consumptions_no_delete
BEFORE DELETE ON cognitive_data_consumptions BEGIN
    SELECT RAISE(ABORT, 'cognitive_data_consumptions are immutable');
END;
CREATE TRIGGER cognitive_data_reconciliations_no_update
BEFORE UPDATE ON cognitive_data_reconciliations BEGIN
    SELECT RAISE(ABORT, 'cognitive_data_reconciliations are immutable');
END;
CREATE TRIGGER cognitive_data_reconciliations_no_delete
BEFORE DELETE ON cognitive_data_reconciliations BEGIN
    SELECT RAISE(ABORT, 'cognitive_data_reconciliations are immutable');
END;
CREATE TRIGGER cognitive_state_revisions_no_update
BEFORE UPDATE ON cognitive_state_revisions BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable');
END;
CREATE TRIGGER cognitive_state_revisions_no_delete
BEFORE DELETE ON cognitive_state_revisions BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable');
END;
CREATE TRIGGER cognitive_state_outbox_no_update
BEFORE UPDATE ON cognitive_state_outbox BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_outbox is immutable');
END;
CREATE TRIGGER cognitive_state_outbox_no_delete
BEFORE DELETE ON cognitive_state_outbox BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_outbox is immutable');
END;
CREATE TRIGGER cognitive_state_effect_receipts_no_update
BEFORE UPDATE ON cognitive_state_effect_receipts BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
END;
CREATE TRIGGER cognitive_state_effect_receipts_no_delete
BEFORE DELETE ON cognitive_state_effect_receipts BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
END;
CREATE TRIGGER cognitive_state_migration_quarantine_no_update
BEFORE UPDATE ON cognitive_state_migration_quarantine BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_migration_quarantine is immutable');
END;
CREATE TRIGGER cognitive_state_migration_quarantine_no_delete
BEFORE DELETE ON cognitive_state_migration_quarantine BEGIN
    SELECT RAISE(ABORT, 'cognitive_state_migration_quarantine is immutable');
END;
"""

# Exact v4/v3/v2 DDL is retained as migration input evidence.  Do not
# regenerate any contract from a caller or infer it from a live database.
LEGACY_CANONICAL_V4_DDL = CANONICAL_DDL.replace(
    "CHECK(status IN ('consumed', 'dead_letter', 'skipped', 'in_progress'))",
    "CHECK(status IN ('consumed', 'dead_letter', 'skipped'))",
)
LEGACY_CANONICAL_V3_DDL = LEGACY_CANONICAL_V4_DDL.replace(
    ",\n        'training_admission_record', 'training_run_record'",
    "",
)
LEGACY_CANONICAL_V2_DDL = (
    LEGACY_CANONICAL_V3_DDL.replace(
        ", 'feedback_attribution_record'",
        "",
    )
    .replace(
        "CREATE INDEX idx_cognitive_state_outbox_command_type\n"
        "    ON cognitive_state_outbox(command_type, revision_id);\n"
        "CREATE INDEX idx_cognitive_state_outbox_pending_order\n"
        "    ON cognitive_state_outbox(created_at, command_id);\n",
        "",
    )
    .replace(
        FEEDBACK_COMMAND_ATTEMPT_DDL,
        "",
    )
)
