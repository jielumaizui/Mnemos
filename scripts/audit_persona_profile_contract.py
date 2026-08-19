#!/usr/bin/env python3
"""Audit the isolated, seeded persona-v2 structural contract.

This is deliberately not a production-effectiveness audit: it creates an
ephemeral database and seeds a representative authorized profile to exercise
schema, ACL and consumer contracts.  Use
``audit_persona_runtime_effectiveness.py`` for a read-only inspection of an
actual deployed store.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db_utils import render_sql  # noqa: E402

REQUIRED_TABLE_COLUMNS = {
    "profile_signals": {
        "source_event_id",
        "source_identity",
        "source_authority_id",
        "source_authority",
        "source_revision_sha256",
        "source_span_start",
        "source_span_end",
        "source_content_sha256",
        "signal_type",
        "dimension",
        "value",
        "evidence",
        "confidence",
        "privacy_level",
        "observed_at",
        "expires_at",
        "status",
        "access_control",
    },
    "profile_assertions": {
        "assertion_id",
        "current_revision_id",
        "dimension",
        "claim",
        "supporting_signals",
        "contradicting_signals",
        "confidence",
        "privacy_level",
        "last_verified_at",
        "revision_policy",
        "status",
        "access_control",
    },
    "profile_assertion_revisions": {
        "revision_id",
        "assertion_id",
        "revision_number",
        "content_hash",
        "supersedes_revision_id",
        "dimension",
        "claim",
        "supporting_signals",
        "contradicting_signals",
        "confidence",
        "privacy_level",
        "last_verified_at",
        "revision_policy",
        "status",
        "access_control",
        "created_at",
    },
    "profile_assertion_heads": {
        "assertion_id",
        "revision_id",
        "updated_at",
    },
    "profile_usage_log": {
        "consumer",
        "profile_fields_used",
        "profile_revision_ids",
        "matched_assertion_revisions",
        "scope_snapshot",
        "read_purpose",
        "read_authorization_token",
        "action_changed",
        "outcome",
        "user_feedback",
        "request_id",
        "decision_id",
        "baseline_hash",
        "persona_enabled_hash",
        "expected_delta",
        "actual_target_delta",
        "target_receipt",
        "target_receipt_hash",
        "terminal_status",
        "idempotency_key",
        "access_control",
        "created_at",
    },
    "profile_read_authorizations": {
        "token_id",
        "consumer",
        "read_purpose",
        "principal_id",
        "principal_agent",
        "scope_snapshot",
        "authorized_assertion_revisions",
        "assertion_access_hashes",
        "access_control",
        "status",
        "consumed_command_id",
        "issued_at",
        "expires_at",
    },
    "profile_usage_outbox": {
        "command_id",
        "idempotency_key",
        "intent_json",
        "target_receipt_hash",
        "access_control",
        "status",
        "usage_id",
        "attempts",
        "last_error",
        "created_at",
        "updated_at",
    },
}

PRODUCTION_SOURCE_ROOTS = ("core", "integrations", "daemon")

REQUIRED_PROFILE_BUCKETS = {
    "decision_preferences",
    "judgment_standards",
    "behavior_signals",
    "current_goal_state",
    "interaction_contracts",
    "risk_boundaries",
    "negative_feedback",
    "cognitive_flywheel_inputs",
}

AUTHORIZED_CONSUMER_CONTRACTS = {
    "preflight_builder": ("persona_preflight_read", "prompt"),
    "context_search": ("context_search_profile", "ranking"),
    "persona_behavior_prompt": ("persona_behavior_prompt", "prompt"),
}
AUTHORIZED_CONSUMERS = set(AUTHORIZED_CONSUMER_CONTRACTS)
EXPECTED_USAGE_CALLERS = {
    "core/app/context_search_profile.py",
    "core/application/persona.py",
    "integrations/preflight_builder.py",
}

# These former callers had no server-resolved principal and were only writing
# cosmetic usage rows.  Their reads are deliberately disabled rather than
# treating an unscoped background process as the user.
UNSCOPED_CONSUMERS_DISABLED = {
    "quality_gate",
    "auto_healing",
    "cognitive_decision_flywheel",
    "distillation_prompt",
}

FINAL_RENDER_BINDINGS = {
    "core/app/context_search_profile.py": (
        '"baseline_ranking"',
        '"persona_enabled_ranking"',
    ),
    "core/application/persona.py": ('"rendered_assertion_revisions"',),
    "integrations/preflight_builder.py": ('"emitted_assertion_revisions"',),
}


def _profile_usage_render_binding_gaps() -> int:
    """Require both receipt payloads and final-output-before-receipt call order."""

    gaps = sum(
        1
        for relative_path, markers in FINAL_RENDER_BINDINGS.items()
        if any(
            marker not in (ROOT / relative_path).read_text(encoding="utf-8")
            for marker in markers
        )
    )
    search_source = (ROOT / "core/app/context_search.py").read_text(encoding="utf-8")
    final_output_offset = search_source.find("selected = results[:limit]")
    usage_receipt_offset = search_source.find("self._record_authorized_profile_usage(")
    if (
        final_output_offset < 0
        or usage_receipt_offset < 0
        or final_output_offset >= usage_receipt_offset
    ):
        gaps += 1
    return gaps


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _production_method_calls(method_name: str) -> list[str]:
    """Return static production call sites, excluding definitions and fixtures."""

    found: list[str] = []
    for root in PRODUCTION_SOURCE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                found.append(f"{path.relative_to(ROOT)}: parse_error:{exc.__class__.__name__}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == method_name:
                        found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def _production_calls_missing_keywords(
    method_name: str,
    required_keywords: set[str],
) -> list[str]:
    """Find production calls that omit a non-null trusted-context argument."""

    found: list[str] = []
    for root in PRODUCTION_SOURCE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                found.append(f"{path.relative_to(ROOT)}:parse_error:{exc.__class__.__name__}")
                continue
            for node in ast.walk(tree):
                if (
                    not isinstance(node, ast.Call)
                    or not isinstance(node.func, ast.Attribute)
                    or node.func.attr != method_name
                ):
                    continue
                keywords = {
                    str(keyword.arg): keyword.value
                    for keyword in node.keywords
                    if keyword.arg is not None
                }
                if any(
                    keyword not in keywords
                    or (
                        isinstance(keywords[keyword], ast.Constant)
                        and keywords[keyword].value is None
                    )
                    for keyword in required_keywords
                ):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def _profile_assertion_schema_owner_count() -> int:
    pattern = re.compile(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+profile_assertion_revisions\b",
        re.IGNORECASE,
    )
    return sum(
        len(pattern.findall(path.read_text(encoding="utf-8")))
        for path in sorted((ROOT / "core").rglob("*.py"))
    )


def _profile_schema_migration_contract_metrics() -> dict[str, int]:
    psyche_source = (ROOT / "core/persona/psyche.py").read_text(encoding="utf-8")
    migration_source = (ROOT / "scripts/reconcile_profile_assertion_revisions.py").read_text(
        encoding="utf-8"
    )
    read_path_safe = (
        all(
            marker in psyche_source
            for marker in (
                "initialize_schema: bool = False",
                "if initialize_schema:",
                "validate_cognitive_profile_runtime_schema(self._pool.get_conn())",
                "SignalStore schema requires explicit reconciliation",
            )
        )
        and 'object.__getattribute__(self, "_init_db")()' not in psyche_source
    )
    reviewed_plan_required = all(
        marker in migration_source
        for marker in (
            "if not expected_plan_hash:",
            "expected plan hash does not match locked source state",
            "--expected-plan-hash",
            "offline_migration_lock",
        )
    )
    collision_safe = "os.O_CREAT | os.O_EXCL" in migration_source
    recovery_safe = all(
        marker in migration_source
        for marker in (
            "_restore_drill(",
            "second_apply_changed_rows",
            "conn.rollback()",
        )
    )
    return {
        "read_path_schema_mutation": int(not read_path_safe),
        "migration_without_reviewed_plan_hash": int(not reviewed_plan_required),
        "backup_generation_collision": int(not collision_safe),
        "rollback_or_restore_mismatch": int(not recovery_safe),
    }


def _seed_complete_profile(store: Any) -> dict[str, str]:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.persona.psyche import ProfileAssertion, ProfileSignal, ProfileUsageLog

    dimensions = {
        "decision_preference": "用户倾向选择能被验证命令证明的实现方案。",
        "judgment_standard": "用户判断质量时要求证据、测试结果和风险边界。",
        "behavior_pattern": "用户反复要求修复后深度审核历史遗留和新增问题。",
        "current_goal_state": "用户当前目标是逐项关闭满分问题清单。",
        "interaction_contract": "每个问题必须修复、测试、同步文档并提交本地仓库。",
        "risk_boundary": "不得重建已删除的暗知识或量子纠缠历史模块。",
        "negative_feedback": "遇到纠错、忽略、打断或返工时必须形成可反驳信号。",
        "cognitive_flywheel_input": "画像断言必须反馈给认知决策飞轮影响后续行动。",
    }
    assertion_ids: dict[str, str] = {}
    for index, (dimension, claim) in enumerate(dimensions.items(), start=1):
        signal_id = store.record_profile_signal(
            ProfileSignal(
                source_event_id=f"session:audit-profile-v2:{index}",
                signal_type="explicit_preference",
                dimension=dimension,
                value=claim,
                evidence=f"audit evidence for {dimension}",
                confidence=0.9,
                privacy_level="local",
                observed_at="2026-07-05T12:00:00",
                expires_at="2026-10-05T12:00:00",
                status="active",
                access_control=make_cognitive_access_envelope(
                    owner_principal_id="mcp:codex:persona-contract",
                    owner_agent="codex",
                    scope_type="session",
                    scope_id="persona-contract-session",
                    session_id="persona-contract-session",
                    purposes=(
                        "persona_preflight_read",
                        "context_search_profile",
                        "persona_summary_read",
                        "persona_behavior_prompt",
                        "persona_usage_metrics",
                    ),
                    consent_provenance_refs=(f"session:audit-profile-v2:{index}",),
                    sensitivity="sensitive",
                    retention_policy="persona_retention",
                    source_acl_lineage=(f"sha256:persona-audit:{index}",),
                    visibility="agent",
                ),
            )
        )
        assertion_id = store.upsert_profile_assertion(
            ProfileAssertion(
                assertion_id=f"pa_audit_{dimension}",
                dimension=dimension,
                claim=claim,
                supporting_signals=[f"profile_signals:{signal_id}"],
                contradicting_signals=[],
                confidence=0.9,
                privacy_level="local",
                last_verified_at="2026-07-05T12:00:00",
                revision_policy="revise_on_user_correction_or_contradiction",
                status="active",
            )
        )
        assertion_ids[dimension] = assertion_id

    for consumer in AUTHORIZED_CONSUMERS:
        from core.persona.profile_effect import compare_profile_effect

        read_purpose, target_type = AUTHORIZED_CONSUMER_CONTRACTS[consumer]
        matched_revisions = {
            assertion_id: str(
                store.get_profile_assertion_revisions(assertion_id)[-1]["revision_id"]
            )
            for assertion_id in sorted(assertion_ids.values())
        }
        read_principal = PrincipalEnvelope(
            principal_id="mcp:codex:persona-contract",
            agent="codex",
            host_kind="codex",
            capability_id="persona-contract",
            capabilities=frozenset({"memory_read"}),
        )
        read_narrowing = AccessNarrowing(session_id="persona-contract-session")
        _profile, read_access = store.build_authorized_user_cognitive_profile_v2(
            principal=read_principal,
            narrowing=read_narrowing,
            purpose=read_purpose,
            consumer=consumer,
        )
        store.record_profile_usage(
            ProfileUsageLog(
                consumer=consumer,
                profile_fields_used=sorted(assertion_ids.values()),
                read_purpose=read_purpose,
                read_authorization_token=str(read_access["read_authorization_token"]),
                target_receipt=compare_profile_effect(
                    owner=consumer,
                    target_type=target_type,
                    target_id=f"audit_{consumer}_target",
                    matched_assertion_revisions=matched_revisions,
                    baseline_output={"mode": "baseline"},
                    persona_enabled_output={"mode": "persona", "consumer": consumer},
                    expected_delta={"kind": "contract_fixture"},
                    receipt_id=f"profile-contract:{consumer}",
                ),
                outcome=f"{consumer}_profile_consumed",
                user_feedback="",
            ),
            principal=read_principal,
            narrowing=read_narrowing,
        )
    return assertion_ids


def audit_persona_profile_contract(*, strict: bool = False) -> list[str]:
    from core.access_policy import AccessNarrowing, PrincipalEnvelope
    from core.cognitive.access_control import validate_cognitive_access_envelope
    from core.persona.cognitive_profile import inspect_cognitive_profile_runtime_schema
    from core.persona.psyche import SignalStore
    from core.persona.profile_assertion_schema import inspect_profile_assertion_schema

    errors: list[str] = []
    source_event_replay_duplicates = 0
    assertion_with_noncanonical_signal_ref = 0
    unauthorized_persona_update = 0
    mutable_assertion_revision = 0
    multiple_active_assertion_heads = 0
    correction_without_supersedes = 0
    projection_head_mismatch = 0
    schema_owner_count = _profile_assertion_schema_owner_count()
    migration_contract_metrics = _profile_schema_migration_contract_metrics()
    partial_profile_schema_migration = 0
    with tempfile.TemporaryDirectory(prefix="mnemos-profile-contract-") as tmp:
        store = SignalStore(
            db_path=Path(tmp) / "user_signals.db",
            initialize_schema=True,
        )
        conn = store._pool.get_conn()

        for table, required_columns in REQUIRED_TABLE_COLUMNS.items():
            columns = _table_columns(conn, table)
            missing = sorted(required_columns - columns)
            if missing:
                errors.append(f"{table} missing columns: {missing}")

        assertion_ids = _seed_complete_profile(store)
        principal = PrincipalEnvelope(
            principal_id="mcp:codex:persona-contract",
            agent="codex",
            host_kind="codex",
            capability_id="persona-contract",
            capabilities=frozenset({"memory_read"}),
        )
        narrowing = AccessNarrowing(session_id="persona-contract-session")
        profile, profile_access = store.build_authorized_user_cognitive_profile_v2(
            principal=principal,
            narrowing=narrowing,
            purpose="persona_preflight_read",
        )
        if profile_access.get("authorized_count") != len(REQUIRED_PROFILE_BUCKETS):
            errors.append("authorized profile count does not cover required buckets")
        if profile.get("schema_version") != "mnemos.user_cognitive_profile.v2":
            errors.append("profile schema_version is not mnemos.user_cognitive_profile.v2")
        if profile.get("status") != "active":
            errors.append("profile status is not active after seeded assertions")
        if not profile.get("evidence_refs"):
            errors.append("profile has no evidence_refs")
        if profile.get("confidence", 0) <= 0:
            errors.append("profile confidence was not aggregated")

        for bucket in sorted(REQUIRED_PROFILE_BUCKETS):
            entries = profile.get(bucket) or []
            if not entries:
                errors.append(f"profile bucket {bucket} is empty")
                continue
            entry = entries[0]
            for field in (
                "assertion_id",
                "claim",
                "confidence",
                "privacy_level",
                "evidence_refs",
                "revision_policy",
                "last_verified_at",
            ):
                if entry.get(field) in (None, "", []):
                    errors.append(f"profile bucket {bucket} entry missing {field}")

        active_assertions = store.get_profile_assertions(
            status="active",
            principal=principal,
            narrowing=narrowing,
            purpose="persona_preflight_read",
        )
        for assertion in active_assertions:
            if not assertion.get("supporting_signals"):
                errors.append(f"{assertion['assertion_id']} has no supporting_signals")
            if "contradicting_signals" not in assertion:
                errors.append(f"{assertion['assertion_id']} has no contradicting_signals")
            if assertion.get("privacy_level") != "local":
                errors.append(f"{assertion['assertion_id']} has unexpected privacy_level")
            if not assertion.get("revision_policy"):
                errors.append(f"{assertion['assertion_id']} has no revision_policy")

        # SignalStore releases transient connections after each public call;
        # reacquire before running the structural evidence queries below.
        conn = store._pool.get_conn()
        schema_state = inspect_profile_assertion_schema(conn)
        if not schema_state.ok:
            errors.append("profile assertion schema drift: " + ", ".join(schema_state.errors))
        runtime_schema_state = inspect_cognitive_profile_runtime_schema(conn)
        partial_profile_schema_migration = int(not runtime_schema_state["ok"])
        if partial_profile_schema_migration:
            errors.append(
                "profile runtime schema drift: " + ", ".join(runtime_schema_state["errors"])
            )
        mutation_target = conn.execute(
            "SELECT revision_id FROM profile_assertion_revisions LIMIT 1"
        ).fetchone()
        if mutation_target is not None:
            for operation in (
                "UPDATE profile_assertion_revisions SET claim='forged' WHERE revision_id=?",
                "DELETE FROM profile_assertion_revisions WHERE revision_id=?",
            ):
                conn.execute("SAVEPOINT assertion_immutability_probe")
                try:
                    conn.execute(operation, (str(mutation_target[0]),))
                    mutable_assertion_revision += 1
                except sqlite3.IntegrityError:
                    pass
                finally:
                    conn.execute("ROLLBACK TO assertion_immutability_probe")
                    conn.execute("RELEASE assertion_immutability_probe")
        multiple_active_assertion_heads = int(conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT assertion_id FROM profile_assertion_heads
                    GROUP BY assertion_id HAVING COUNT(*) != 1
                    UNION ALL
                    SELECT revision_id FROM profile_assertion_heads
                    GROUP BY revision_id HAVING COUNT(*) > 1
                )
                """).fetchone()[0])
        correction_without_supersedes = int(conn.execute("""
                SELECT COUNT(*)
                FROM profile_assertion_revisions AS child
                LEFT JOIN profile_assertion_revisions AS parent
                  ON parent.revision_id=child.supersedes_revision_id
                 AND parent.assertion_id=child.assertion_id
                 AND parent.revision_number=child.revision_number - 1
                WHERE (child.revision_number=1 AND child.supersedes_revision_id IS NOT NULL)
                   OR (child.revision_number>1 AND parent.revision_id IS NULL)
                """).fetchone()[0])
        projection_head_mismatch = int(conn.execute("""
                SELECT COUNT(*)
                FROM profile_assertions AS current
                LEFT JOIN profile_assertion_heads AS head
                  ON head.assertion_id=current.assertion_id
                LEFT JOIN profile_assertion_revisions AS revision
                  ON revision.revision_id=head.revision_id
                 AND revision.assertion_id=head.assertion_id
                WHERE head.revision_id IS NULL
                   OR current.current_revision_id IS NOT head.revision_id
                   OR current.dimension IS NOT revision.dimension
                   OR current.claim IS NOT revision.claim
                   OR current.supporting_signals IS NOT revision.supporting_signals
                   OR current.contradicting_signals IS NOT revision.contradicting_signals
                   OR current.confidence IS NOT revision.confidence
                   OR current.privacy_level IS NOT revision.privacy_level
                   OR current.last_verified_at IS NOT revision.last_verified_at
                   OR current.revision_policy IS NOT revision.revision_policy
                   OR current.status IS NOT revision.status
                   OR current.access_control IS NOT revision.access_control
                """).fetchone()[0])
        source_event_replay_duplicates = int(conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT source_identity
                    FROM profile_signals
                    WHERE source_identity != ''
                    GROUP BY source_identity
                    HAVING COUNT(*) > 1
                )
                """).fetchone()[0])
        known_signal_ids = {
            int(row[0]) for row in conn.execute("SELECT id FROM profile_signals").fetchall()
        }
        for assertion in active_assertions:
            for reference in assertion.get("supporting_signals") or []:
                signal_id = None
                if isinstance(reference, str) and reference.startswith("profile_signals:"):
                    try:
                        signal_id = int(reference.split(":", 1)[1])
                    except ValueError:
                        signal_id = None
                if signal_id is None or signal_id not in known_signal_ids:
                    assertion_with_noncanonical_signal_ref += 1
        unauthorized_persona_update = int(conn.execute("""
                SELECT COUNT(*)
                FROM profile_signals
                WHERE source_authority_id != ''
                  AND (
                    source_authority != 'explicit_user'
                    OR source_revision_sha256 = ''
                    OR source_content_sha256 = ''
                    OR source_span_start < 0
                    OR source_span_end <= source_span_start
                  )
                """).fetchone()[0])

        metrics = store.get_authorized_profile_usage_metrics(
            days=7,
            principal=principal,
            narrowing=narrowing,
            purpose="persona_usage_metrics",
        )
        missing_consumers = sorted(AUTHORIZED_CONSUMERS - set(metrics["by_consumer"]))
        if missing_consumers:
            errors.append(f"profile usage missing consumers: {missing_consumers}")
        if metrics.get("action_changed_count") != len(AUTHORIZED_CONSUMERS):
            errors.append("profile usage did not record action_changed for authorized consumers")

        unauthenticated_profile = store.build_user_cognitive_profile_v2()
        if unauthenticated_profile.get("profile_assertions"):
            errors.append("uncredentialed profile read returned assertions")

        conn = store._pool.get_conn()
        for table in (
            "profile_signals",
            "profile_assertions",
            "profile_read_authorizations",
            "profile_usage_log",
            "profile_usage_outbox",
        ):
            rows = conn.execute(
                render_sql(
                    "SELECT access_control FROM {table}",
                    identifiers={"table": table},
                )
            ).fetchall()
            if not rows:
                errors.append(f"{table} has no seeded ACL rows")
            for row in rows:
                try:
                    validate_cognitive_access_envelope(json.loads(row[0] or ""))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"{table} has invalid object ACL: {exc}")

        for relative_path in (
            "core/hephaestus/cognitive_value_gate.py",
            "core/kia/ixion.py",
            "core/ops/auto_healing.py",
            "core/hephaestus/prompt_builder.py",
        ):
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            if "build_user_cognitive_profile_v2(" in text:
                errors.append(f"unscoped profile reader remains in {relative_path}")

        if strict:
            existing_ids = {item["assertion_id"] for item in active_assertions}
            missing_ids = sorted(set(assertion_ids.values()) - existing_ids)
            if missing_ids:
                errors.append(f"seeded assertions missing after readback: {missing_ids}")
            if len(profile.get("profile_assertions", [])) < len(REQUIRED_PROFILE_BUCKETS):
                errors.append("strict profile assertion count below required buckets")

    legacy_signal_calls = _production_method_calls("record_profile_signal")
    legacy_assertion_calls = _production_method_calls("upsert_profile_assertion")
    usage_calls = _production_method_calls("record_profile_usage")
    usage_calls_without_resolved_scope = _production_calls_missing_keywords(
        "record_profile_usage",
        {"principal", "narrowing"},
    )
    usage_callers = [call.split(":", 1)[0] for call in usage_calls]
    observed_usage_caller_files = set(usage_callers)
    declared_consumer_without_runtime_route = len(
        EXPECTED_USAGE_CALLERS - observed_usage_caller_files
    )
    disabled_consumer_counted_effective = 0
    for root in PRODUCTION_SOURCE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            disabled_consumer_counted_effective += sum(
                text.count(f'consumer="{consumer}"') + text.count(f"consumer='{consumer}'")
                for consumer in UNSCOPED_CONSUMERS_DISABLED
            )
    usage_recorded_before_final_render = _profile_usage_render_binding_gaps()
    producer_calls = _production_method_calls("record_authorized_profile_evidence")
    producer_entry_calls = [
        call for call in producer_calls if not call.startswith("core/persona/psyche_persona.py:")
    ]
    if legacy_signal_calls:
        errors.append("production legacy profile signal callers: " + ", ".join(legacy_signal_calls))
    if legacy_assertion_calls:
        errors.append(
            "production legacy profile assertion callers: " + ", ".join(legacy_assertion_calls)
        )
    if set(usage_callers) != EXPECTED_USAGE_CALLERS or len(usage_callers) != len(
        EXPECTED_USAGE_CALLERS
    ):
        errors.append(
            "production profile usage caller denominator mismatch: "
            f"observed={sorted(usage_callers)} expected={sorted(EXPECTED_USAGE_CALLERS)}"
        )
    if usage_calls_without_resolved_scope:
        errors.append(
            "production profile usage call missing resolved principal/scope: "
            + ", ".join(usage_calls_without_resolved_scope)
        )
    if declared_consumer_without_runtime_route:
        errors.append(
            "declared consumer without runtime route: " f"{declared_consumer_without_runtime_route}"
        )
    if disabled_consumer_counted_effective:
        errors.append(
            "disabled consumer counted effective: " f"{disabled_consumer_counted_effective}"
        )
    if usage_recorded_before_final_render:
        errors.append(
            "usage recorded before final render: " f"{usage_recorded_before_final_render}"
        )
    if len(producer_entry_calls) != 1 or not producer_entry_calls[0].startswith(
        "core/application/persona.py:"
    ):
        errors.append(
            "production profile signal producer must be the sole application service call"
        )
    if source_event_replay_duplicates:
        errors.append("profile signal source identity has replay duplicates")
    if assertion_with_noncanonical_signal_ref:
        errors.append("profile assertion has noncanonical signal references")
    if unauthorized_persona_update:
        errors.append("profile signal has unauthorized authority binding")
    if mutable_assertion_revision:
        errors.append("profile assertion revision ledger permits mutation")
    if multiple_active_assertion_heads:
        errors.append("profile assertion ledger has multiple active heads")
    if correction_without_supersedes:
        errors.append("profile assertion correction lacks exact supersedes")
    if projection_head_mismatch:
        errors.append("profile assertion current projection does not match its head")
    if schema_owner_count != 1:
        errors.append(f"profile assertion schema owner count is {schema_owner_count}, expected 1")
    for metric, count in migration_contract_metrics.items():
        if count:
            errors.append(f"{metric}:{count}")
    audit_persona_profile_contract.last_phase5_metrics = {
        "production_profile_signal_producer_count": len(producer_entry_calls),
        "producer_delegate_call_count": len(producer_calls) - len(producer_entry_calls),
        "source_event_replay_duplicates": source_event_replay_duplicates,
        "assertion_with_noncanonical_signal_ref": assertion_with_noncanonical_signal_ref,
        "unauthorized_persona_update": unauthorized_persona_update,
        "legacy_signal_call_count": len(legacy_signal_calls),
        "legacy_assertion_call_count": len(legacy_assertion_calls),
        "profile_usage_caller_count": len(usage_callers),
        "usage_call_without_resolved_principal_scope": len(usage_calls_without_resolved_scope),
    }
    audit_persona_profile_contract.last_phase5_assertion_metrics = {
        "mutable_assertion_revision": mutable_assertion_revision,
        "multiple_active_assertion_heads": multiple_active_assertion_heads,
        "correction_without_supersedes": correction_without_supersedes,
        "projection_head_mismatch": projection_head_mismatch,
        "schema_owner_count": schema_owner_count,
        "partial_profile_schema_migration": partial_profile_schema_migration,
        **migration_contract_metrics,
    }
    audit_persona_profile_contract.last_phase5_consumer_metrics = {
        "declared_consumer_without_runtime_route": (declared_consumer_without_runtime_route),
        "disabled_consumer_counted_effective": disabled_consumer_counted_effective,
        "usage_recorded_before_final_render": usage_recorded_before_final_render,
    }

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_persona_profile_contract(strict=args.strict)
    payload = {
        "schema_version": "mnemos.persona_profile_contract.v1",
        "audit_scope": "isolated_seeded_structural_contract",
        "seeded_by_audit": True,
        "certifying": False,
        "ok": not errors,
        "errors": errors,
        "authorized_consumers": sorted(AUTHORIZED_CONSUMERS),
        "unscoped_consumers_disabled": sorted(UNSCOPED_CONSUMERS_DISABLED),
        "required_buckets": sorted(REQUIRED_PROFILE_BUCKETS),
        "phase5_producer_metrics": getattr(
            audit_persona_profile_contract, "last_phase5_metrics", {}
        ),
        "phase5_assertion_metrics": getattr(
            audit_persona_profile_contract,
            "last_phase5_assertion_metrics",
            {},
        ),
        "phase5_consumer_metrics": getattr(
            audit_persona_profile_contract,
            "last_phase5_consumer_metrics",
            {},
        ),
        "usage_call_without_resolved_principal_scope": getattr(
            audit_persona_profile_contract,
            "last_phase5_metrics",
            {},
        ).get("usage_call_without_resolved_principal_scope", 0),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Persona profile contract audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Persona profile contract audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
