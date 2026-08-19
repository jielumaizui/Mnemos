#!/usr/bin/env python3
"""Hermetic physical-effect matrix for COG-043 subject deletion.

The fixture uses production storage owners and the production Wiki EventBus
consumers. Only infrastructure dependencies are injected: an isolated runtime
config and a deterministic, non-network embedding client. No delete handler or
after-oracle is mocked.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cognitive_acl_deletion_effect_audit import (  # noqa: E402
    _acl_decision_matrix,
    _active_acl_inventory,
    _active_subject_residuals,
    _authorized_typed_object_presence,
    _freeze_resurrection_barrier,
    _multi_source_acl_merge_matrix,
    _pre_body_authorization_matrix,
    _projection_residuals,
    _public_acl_rejected,
)
from scripts.cognitive_acl_deletion_effect_contracts import (  # noqa: E402
    EFFECT_MATRIX_SCHEMA_VERSION,
    FAILURE_MODES,
    _SESSION_ID,
    _SUBJECT_SCOPE,
)
from scripts.cognitive_acl_deletion_effect_fixtures import (  # noqa: E402
    _DeterministicEmbeddingClient,
    _MatrixConfig,
    _access_control,
    _commit_cognitive_tombstone,
    _domain_target_counts,
    _effect_matrix_material_action_scope,
    _inject_domain_failure,
    _inject_wiki_consumer_failure,
    _relation_embedding_target_status,
    _scoring_effect_counts,
    _seed_cognitive_state,
    _seed_non_wiki_domains,
    _seed_relation_embedding_target,
    _wait_for_bus,
    _write_wiki_page,
)


def run_effect_matrix(*, failure_mode: str = "") -> dict[str, Any]:
    """Execute the success matrix or one expected, fail-closed target fault."""

    if failure_mode == "wiki_search_index":
        failure_mode = "wiki_consumer:wiki_search_index"
    if failure_mode not in {"", *FAILURE_MODES}:
        raise ValueError(f"unsupported COG-043 failure mode: {failure_mode}")

    from core.cognitive_graph.store import CognitiveGraphStore
    from core.cognitive_graph.updater import CognitiveGraphUpdater
    from core.mnemos_bus import Event, EventBus, HandlerOutcome
    from core.privacy.data_ownership import (
        DATA_DOMAINS,
        DataOwnershipManager,
        DeletionProof,
    )
    from core.wiki_projection_lifecycle import (
        DEFAULT_REQUIRED_CONSUMERS,
        WikiProjectionLedger,
    )
    from core.wiki_projection_publisher import publish_wiki_mutation
    from daemon.wiki_projection_handlers import register_wiki_projection_handlers

    with tempfile.TemporaryDirectory(prefix="mnemos-cog043-effect-matrix-") as temp_dir:
        config = _MatrixConfig(Path(temp_dir))
        embedding_client = _DeterministicEmbeddingClient()
        page = _write_wiki_page(config)
        ledger = WikiProjectionLedger(config.database_dir / "wiki_projection.db")
        create_mutation = ledger.record_mutation(page, mutation_type="create")

        with ExitStack() as stack:
            stack.enter_context(patch("core.config.get_config", return_value=config))
            stack.enter_context(patch("core.app.context_search.get_config", return_value=config))
            stack.enter_context(patch("core.mnemos_bus.get_config", return_value=config))
            stack.enter_context(
                patch("core.wiki_projection_lifecycle.get_config", return_value=config)
            )
            stack.enter_context(
                patch(
                    "core.wiki_projection_lifecycle._default_db_path",
                    return_value=config.database_dir / "wiki_projection.db",
                )
            )
            stack.enter_context(
                patch(
                    "core.mnemos_bus.resolve_wiki_projection_db_path",
                    return_value=config.database_dir / "wiki_projection.db",
                )
            )
            stack.enter_context(patch("core.trust.config.get_config", return_value=config))
            stack.enter_context(_effect_matrix_material_action_scope(config))

            bus = EventBus(config=config)
            stack.enter_context(patch("core.mnemos_bus._global_bus", bus))
            stack.enter_context(
                patch(
                    "core.mnemos_bus.publish_event",
                    side_effect=lambda event_type, agent, payload, trace_id="", subject_provenance=None: bus.publish(
                        Event(
                            event_type=event_type,
                            source=agent,
                            payload=payload,
                            trace_id=trace_id,
                            subject_provenance=subject_provenance,
                        )
                    ),
                )
            )
            register_wiki_projection_handlers(
                bus,
                config,
                embedding_client=embedding_client,
            )
            cognitive_store = CognitiveGraphStore(str(config.database_dir / "cognitive_graph.db"))
            CognitiveGraphUpdater(store=cognitive_store, bus=bus).subscribe()
            bus.subscribe(
                "cog043_effect_matrix_subject",
                lambda _event: HandlerOutcome.ack("cog043_effect_matrix_subject"),
                consumer_id="cog043_effect_matrix_subject",
            )

            publish_wiki_mutation(
                create_mutation,
                ledger=ledger,
                source="cog043_effect_matrix_seed",
                event_bus=bus,
            )
            bus.start_dispatch()
            initial_bus = _wait_for_bus(bus)

            access_control = _access_control()
            state_store, state_revision_id, user_correction = _seed_cognitive_state(
                config, access_control
            )
            seeded = _seed_non_wiki_domains(
                config,
                access_control=access_control,
            )
            seeded.update(
                _seed_relation_embedding_target(
                    config,
                    page=page,
                    embedding_client=embedding_client,
                )
            )
            bus.publish(seeded["event"])
            _wait_for_bus(bus)
            bus.stop_dispatch()
            relation_target_before = _relation_embedding_target_status(
                config,
                seeded=seeded,
            )
            acl_decisions = _acl_decision_matrix(access_control)
            acl_merge = _multi_source_acl_merge_matrix(access_control)
            pre_body_matrix = _pre_body_authorization_matrix(
                config,
                state_store=state_store,
                seeded=seeded,
            )
            acl_inventory = _active_acl_inventory(config)

            manager = DataOwnershipManager(config, event_bus=bus)
            manager.freeze(_SUBJECT_SCOPE)
            freeze_barrier = _freeze_resurrection_barrier(
                config,
                access_control=access_control,
                event_bus=bus,
            )
            snapshot_ref = manager.create_delete_snapshot(
                _SUBJECT_SCOPE,
                retention_days=30,
            ).snapshot_id
            failure_kind, _, failure_target = failure_mode.partition(":")
            if failure_kind == "domain":
                _inject_domain_failure(stack, DataOwnershipManager, failure_target)
            first = manager.delete(
                _SUBJECT_SCOPE,
                dry_run=False,
                apply=True,
                confirm=True,
                snapshot_ref=snapshot_ref,
            )
            if not isinstance(first, DeletionProof):
                raise RuntimeError("effect matrix apply returned a dry-run deletion request")

            pending_before_restart = int(bus.stats()["pending"])
            bus.close()
            bus = EventBus(config=config)
            register_wiki_projection_handlers(
                bus,
                config,
                embedding_client=embedding_client,
            )
            CognitiveGraphUpdater(
                store=CognitiveGraphStore(str(config.database_dir / "cognitive_graph.db")),
                bus=bus,
            ).subscribe()
            bus.subscribe(
                "cog043_effect_matrix_subject",
                lambda _event: HandlerOutcome.ack("cog043_effect_matrix_subject"),
                consumer_id="cog043_effect_matrix_subject",
            )
            restart_bus_stats = bus.stats()
            manager = DataOwnershipManager(config, event_bus=bus)
            if failure_kind == "wiki_consumer":
                _inject_wiki_consumer_failure(bus, failure_target, HandlerOutcome)
            bus.start_dispatch()
            final_bus = _wait_for_bus(bus)
            bus.stop_dispatch()
            if failure_mode != "domain:cognitive_state":
                _commit_cognitive_tombstone(state_store)

            final = manager.delete(
                _SUBJECT_SCOPE,
                dry_run=False,
                apply=True,
                confirm=True,
                snapshot_ref=snapshot_ref,
            )
            if not isinstance(final, DeletionProof):
                raise RuntimeError("effect matrix replay returned a dry-run deletion request")
            user_correction["current_revision_after_delete"] = (
                state_store.current_revision(
                    "cognitive_update_receipt",
                    "cog043-effect-matrix-state",
                )
                is not None
            )
            authorized_after_delete = _authorized_typed_object_presence(
                config,
                state_store=state_store,
                seeded=seeded,
            )
            authorization_revocation = {
                "domain_denominator": sorted(pre_body_matrix["allowed"]),
                "authorized_before_delete": dict(pre_body_matrix["allowed"]),
                "authorized_after_delete": authorized_after_delete,
                "post_delete_authorization_leak": sum(
                    int(value) for value in authorized_after_delete.values()
                ),
            }
            domain_target_counts = _domain_target_counts(first, final)
            scoring_effect_counts = _scoring_effect_counts(first, final)
            active_residuals = _active_subject_residuals(
                config,
                page=page,
                state_store=state_store,
                state_revision_id=state_revision_id,
                seeded=seeded,
            )
            projection_residuals = _projection_residuals(
                config,
                page,
                seeded=seeded,
            )
            relation_target_after = _relation_embedding_target_status(
                config,
                seeded=seeded,
            )
            relation_target_complete = (
                relation_target_before["relation_count"] > 0
                and relation_target_before["embedding_count"] > 0
                and sum(relation_target_after.values()) == 0
            )
            with sqlite3.connect(config.database_dir / "wiki_projection.db") as conn:
                relation_failure_reasons = {
                    str(row[0]): str(row[1])
                    for row in conn.execute("""SELECT mutation_id, reason FROM projection_receipts
                           WHERE consumer='relation_embeddings'
                             AND outcome NOT IN ('ack', 'noop')""").fetchall()
                }
            projection_gap_details = [
                {
                    "mutation_id": str(gap.get("mutation_id") or ""),
                    "mutation_type": str(gap.get("mutation_type") or ""),
                    "missing_consumers": list(gap.get("missing_consumers") or ()),
                    "failed_consumers": list(gap.get("failed_consumers") or ()),
                    "relation_embedding_reason": relation_failure_reasons.get(
                        str(gap.get("mutation_id") or ""), ""
                    ),
                }
                for gap in WikiProjectionLedger(config.database_dir / "wiki_projection.db")
                .reconciliation_report()
                .get("gaps", [])
            ]
            active_subject_rows = sum(active_residuals.values())
            derived_projection_gap = sum(projection_residuals.values())
            verified_without_physical_effect = int(
                final.status == "verified"
                and (
                    active_subject_rows > 0
                    or derived_projection_gap > 0
                    or any(count <= 0 for count in domain_target_counts.values())
                    or not relation_target_complete
                )
            )
            wiki_result = final.verification_results.get("wiki", {})
            wiki_terminal = (
                wiki_result.get("verified") is True
                and int(wiki_result.get("pending_required_consumer_count") or 0) == 0
            )
            snapshot_verification = dict(final.verification_results.get("snapshot", {}))
            snapshot_retained = (
                snapshot_verification.get("valid") is True
                and snapshot_verification.get("retention_status") == "retained_until"
            )
            from core.backup.snapshot_manager import MnemosSnapshotManager

            snapshot_manager = MnemosSnapshotManager(config)
            expiry_moment = datetime.fromisoformat(
                str(snapshot_verification["retention_expires_at"])
            ) + timedelta(seconds=1)
            expired_verification = snapshot_manager.verify_data_delete_snapshot(
                snapshot_ref,
                scope_kind="session",
                scope_value=_SESSION_ID,
                now=expiry_moment,
            )
            expired_preview = snapshot_manager.prune_expired_data_delete_snapshots(
                now=expiry_moment,
                apply=False,
            )
            expired_snapshot_lifecycle = {
                "verification_valid": expired_verification["valid"],
                "retention_status": expired_verification["retention_status"],
                "retained_until_explicit_prune": (
                    snapshot_manager.root_dir / snapshot_ref / "manifest.json"
                ).is_file(),
                "explicit_prune_candidate": snapshot_ref
                in expired_preview["candidate_snapshot_ids"],
            }
            acl_unknown = int(
                first.verification_results.get("wiki", {}).get("acl_unknown_count") or 0
            )
            default_public = 0 if _public_acl_rejected() else 1
            if failure_mode:
                failure_result = (
                    final.verification_results.get(failure_target, {})
                    if failure_kind == "domain"
                    else wiki_result
                )
                failure_receipt_nonterminal = final.status in {"partially_deleted", "blocked"} and (
                    failure_kind == "wiki_consumer" or failure_result.get("verified") is not True
                )
                ok = (
                    final.status in {"partially_deleted", "blocked"}
                    and verified_without_physical_effect == 0
                    and failure_receipt_nonterminal
                    and snapshot_retained
                    and expired_snapshot_lifecycle
                    == {
                        "verification_valid": False,
                        "retention_status": "expired",
                        "retained_until_explicit_prune": True,
                        "explicit_prune_candidate": True,
                    }
                    and acl_decisions["cross_scope_leak"] == 0
                    and acl_decisions["authorized_success"] == 1
                    and pre_body_matrix["pre_body_authorization_gap"] == 0
                    and acl_inventory["active_acl_lineage_gap"] == 0
                    and acl_inventory["active_acl_authorization_gap"] == 0
                    and not acl_inventory["coverage_gap"]
                    and all(acl_merge.values())
                    and freeze_barrier["resurrection_gap"] == 0
                    and all(
                        bool(value)
                        for key, value in user_correction.items()
                        if key
                        not in {
                            "original_revision_id",
                            "corrected_revision_id",
                            "current_revision_after_delete",
                        }
                    )
                    and (
                        failure_mode == "domain:cognitive_state"
                        or not user_correction["current_revision_after_delete"]
                    )
                    and (failure_kind == "domain" or derived_projection_gap > 0)
                )
            else:
                ok = (
                    first.status == "partially_deleted"
                    and final.status == "verified"
                    and acl_unknown == 0
                    and default_public == 0
                    and verified_without_physical_effect == 0
                    and active_subject_rows == 0
                    and derived_projection_gap == 0
                    and relation_target_complete
                    and all(count > 0 for count in domain_target_counts.values())
                    and wiki_terminal
                    and snapshot_retained
                    and expired_snapshot_lifecycle
                    == {
                        "verification_valid": False,
                        "retention_status": "expired",
                        "retained_until_explicit_prune": True,
                        "explicit_prune_candidate": True,
                    }
                    and acl_decisions["cross_scope_leak"] == 0
                    and acl_decisions["authorized_success"] == 1
                    and pre_body_matrix["pre_body_authorization_gap"] == 0
                    and acl_inventory["active_acl_lineage_gap"] == 0
                    and acl_inventory["active_acl_authorization_gap"] == 0
                    and not acl_inventory["coverage_gap"]
                    and all(count > 0 for count in scoring_effect_counts.values())
                    and all(acl_merge.values())
                    and freeze_barrier["resurrection_gap"] == 0
                    and all(
                        bool(value)
                        for key, value in user_correction.items()
                        if key
                        not in {
                            "original_revision_id",
                            "corrected_revision_id",
                            "current_revision_after_delete",
                        }
                    )
                    and not user_correction["current_revision_after_delete"]
                    and authorization_revocation["post_delete_authorization_leak"] == 0
                )
            bus.close()

        return {
            "schema_version": EFFECT_MATRIX_SCHEMA_VERSION,
            "ok": ok,
            "failure_mode": failure_mode,
            "failure_kind": failure_kind,
            "failure_target": failure_target,
            "required_domains": sorted(DATA_DOMAINS),
            "required_wiki_consumers": list(DEFAULT_REQUIRED_CONSUMERS),
            "first_delete_status": first.status,
            "final_delete_status": final.status,
            "final_remaining_domains": list(
                final.verification_results.get("remaining_unimplemented_domains", ())
            ),
            "final_domain_statuses": {
                domain: str(final.verification_results.get(domain, {}).get("status") or "")
                for domain in sorted(DATA_DOMAINS)
            },
            "acl_unknown": acl_unknown,
            "default_public": default_public,
            "verified_without_physical_effect": verified_without_physical_effect,
            "active_subject_rows_after_verified": (
                active_subject_rows if final.status == "verified" else 0
            ),
            "active_subject_residuals": active_residuals,
            "derived_projection_gap": derived_projection_gap,
            "derived_projection_residuals": projection_residuals,
            "relation_embedding_target": {
                "before_relation_count": relation_target_before["relation_count"],
                "before_embedding_count": relation_target_before["embedding_count"],
                "before_outbox_count": relation_target_before["outbox_count"],
                "after_relation_count": relation_target_after["relation_count"],
                "after_embedding_count": relation_target_after["embedding_count"],
                "after_outbox_count": relation_target_after["outbox_count"],
            },
            "projection_gap_details": projection_gap_details,
            "domain_target_counts": domain_target_counts,
            "scoring_object_effect_counts": scoring_effect_counts,
            "wiki_required_consumers_terminal": wiki_terminal,
            "snapshot_verification": snapshot_verification,
            "snapshot_retained": snapshot_retained,
            "expired_snapshot_lifecycle": expired_snapshot_lifecycle,
            "acl_decision_matrix": acl_decisions,
            "multi_source_acl_merge": acl_merge,
            "freeze_resurrection_barrier": freeze_barrier,
            "user_correction": user_correction,
            "authorization_revocation": authorization_revocation,
            "pre_body_authorization_matrix": pre_body_matrix,
            "active_acl_inventory": acl_inventory,
            "event_bus": {
                "initial_dead_letters": initial_bus["dead_letters"],
                "final_dead_letters": final_bus["dead_letters"],
            },
            "restart_recovery": {
                "event_bus_reopened": True,
                "pending_before_restart": pending_before_restart,
                "pending_after_restart": restart_bus_stats["pending"],
                "processing_after_restart": restart_bus_stats["processing"],
                "pending_after_dispatch": final_bus["pending"],
                "processing_after_dispatch": final_bus["processing"],
            },
        }


if __name__ == "__main__":
    print(json.dumps(run_effect_matrix(), ensure_ascii=False, indent=2, sort_keys=True))
