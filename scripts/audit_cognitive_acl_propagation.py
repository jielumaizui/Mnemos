#!/usr/bin/env python3
"""Audit object ACL and deletion-propagation closure for cognitive data.

This audit intentionally does not equate a typed request or an event publish
with a completed privacy effect.  It reports the exact domains that still lack
an owner, plus ANN/cache propagation gaps, so ``--strict`` cannot turn a
partial COG-043 repair into a green release signal.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.access_control import make_cognitive_access_envelope  # noqa: E402
from core.embeddings.cache import EmbeddingCache  # noqa: E402
from core.privacy.data_ownership import DATA_DOMAINS  # noqa: E402
from core.wiki_projection_lifecycle import (  # noqa: E402
    DEFAULT_REQUIRED_CONSUMERS,
    WikiProjectionLedger,
)
from scripts.cognitive_acl_deletion_effect_matrix import (  # noqa: E402
    FAILURE_MODES,
    run_effect_matrix,
)


REPORT_SCHEMA_VERSION = "mnemos.cognitive_acl_propagation_audit.v1"
_PHYSICAL_DELETE_OWNERS = frozenset(
    {
        "raw",
        "consumer_access_log",
        "evidence_refs",
        "reflection",
        "persona",
        "cognitive_graph",
        "wiki",
        "embedding_cache",
        "model_call_ledger",
        "agent_source_metadata",
        "metadata",
        "scoring",
        "observation",
    }
)
_TOMBSTONE_ONLY_OWNERS = frozenset({"action_ledger", "cognitive_state"})
_REQUIRED_OBJECT_TYPE_DENOMINATOR = {
    "scoring": (
        "training_queue",
        "ground_truth",
        "search_session",
        "feedback_event",
        "model",
        "bayesian_state",
        "bayesian_feedback",
        "feedback_prompt",
    ),
    "reflection": ("record", "shift", "layer5"),
    "cognitive_graph": ("relation", "node", "outbox"),
}


def _cognitive_access_public_rejected() -> bool:
    """Prove the typed ACL owner cannot mint a public cognitive object."""

    try:
        make_cognitive_access_envelope(
            owner_principal_id="audit:principal",
            owner_agent="codex",
            scope_type="session",
            scope_id="audit-session",
            session_id="audit-session",
            project="mnemos",
            purposes=("audit_read",),
            consent_provenance_refs=("sha256:" + "a" * 64,),
            sensitivity="sensitive",
            retention_policy="audit",
            source_acl_lineage=("sha256:" + "b" * 64,),
            visibility="public",
        )
    except ValueError:
        return True
    return False


def _wiki_receipt_schema_present() -> bool:
    """Verify the formal Wiki owner creates the typed receipt schema."""

    with tempfile.TemporaryDirectory(prefix="mnemos-cog-acl-audit-") as temp_dir:
        ledger = WikiProjectionLedger(Path(temp_dir) / "wiki_projection.db")
        return ledger.subject_deletion_schema_present()


def build_report() -> dict[str, Any]:
    """Build a body-free COG-043 closure report from canonical owners."""

    unimplemented_domains = sorted(
        set(DATA_DOMAINS) - _PHYSICAL_DELETE_OWNERS - _TOMBSTONE_ONLY_OWNERS
    )
    cache_owner_present = hasattr(EmbeddingCache, "delete_subject_scope")
    public_rejected = _cognitive_access_public_rejected()
    wiki_receipt_schema_present = _wiki_receipt_schema_present()

    effect_matrix: dict[str, Any] = {}
    failure_probes: dict[str, dict[str, Any]] = {}
    matrix_error = ""
    failure_probe_errors: dict[str, str] = {}
    try:
        effect_matrix = run_effect_matrix()
    except (OSError, RuntimeError, TimeoutError, ValueError, sqlite3.Error) as exc:
        matrix_error = type(exc).__name__
    for failure_mode in FAILURE_MODES:
        try:
            failure_probes[failure_mode] = run_effect_matrix(
                failure_mode=failure_mode
            )
        except (OSError, RuntimeError, TimeoutError, ValueError, sqlite3.Error) as exc:
            failure_probe_errors[failure_mode] = type(exc).__name__

    adapter_coverage = {
        domain: bool(
            int(effect_matrix.get("domain_target_counts", {}).get(domain, 0)) > 0
            and effect_matrix.get("final_domain_statuses", {}).get(domain)
        )
        for domain in sorted(DATA_DOMAINS)
    }
    missing_adapter_owners = sorted(
        domain for domain, present in adapter_coverage.items() if not present
    )
    ann_owner_present = bool(
        effect_matrix.get("wiki_required_consumers_terminal")
        and effect_matrix.get("derived_projection_residuals", {}).get(
            "wiki_search_index"
        )
        == 0
        and failure_probes.get("wiki_consumer:wiki_search_index", {}).get("ok")
    )

    errors: list[str] = []
    if matrix_error:
        errors.append("hermetic deletion effect matrix failed: " + matrix_error)
    elif not effect_matrix.get("ok"):
        errors.append("hermetic deletion effect matrix did not close every domain")
    acl_runtime = effect_matrix.get("acl_decision_matrix", {})
    pre_body_runtime = effect_matrix.get("pre_body_authorization_matrix", {})
    acl_inventory = effect_matrix.get("active_acl_inventory", {})
    runtime_object_denominator = acl_inventory.get("object_type_denominator", {})
    active_counts = acl_inventory.get("active_counts", {})
    missing_object_types: list[str] = []
    for domain, object_types in _REQUIRED_OBJECT_TYPE_DENOMINATOR.items():
        declared = set(runtime_object_denominator.get(domain, ()))
        for object_type in object_types:
            inventory_key = f"{domain}_{object_type}"
            if object_type not in declared or int(active_counts.get(inventory_key) or 0) <= 0:
                missing_object_types.append(inventory_key)
    if int(acl_runtime.get("cross_scope_leak") or 0):
        errors.append("cross-scope cognitive retrieval leak detected")
    if int(pre_body_runtime.get("pre_body_authorization_gap") or 0):
        errors.append("cognitive body can be hydrated before authorization")
    if int(acl_inventory.get("active_acl_lineage_gap") or 0):
        errors.append("active derived objects have invalid ACL lineage")
    if int(acl_inventory.get("active_acl_authorization_gap") or 0):
        errors.append("active objects have unresolved scope or consent")
    if acl_inventory.get("coverage_gap"):
        errors.append(
            "active ACL inventory fixture gaps: "
            + ", ".join(acl_inventory["coverage_gap"])
        )
    if missing_object_types:
        errors.append(
            "object-level ACL fixture gaps: " + ", ".join(sorted(missing_object_types))
        )
    if failure_probe_errors:
        errors.append(
            "target-failure probes failed: "
            + ", ".join(
                f"{mode}={failure_probe_errors[mode]}"
                for mode in sorted(failure_probe_errors)
            )
        )
    failed_closed_gaps = sorted(
        mode
        for mode in FAILURE_MODES
        if not failure_probes.get(mode, {}).get("ok")
    )
    if failed_closed_gaps:
        errors.append(
            "targets not proven fail-closed: " + ", ".join(failed_closed_gaps)
        )
    if missing_adapter_owners:
        errors.append("missing canonical deletion adapters: " + ", ".join(missing_adapter_owners))
    if unimplemented_domains:
        errors.append(
            "data domains without a physical/tombstone owner: "
            + ", ".join(unimplemented_domains)
        )
    if not ann_owner_present:
        errors.append("ANN index lifecycle deletion consumer contract is missing")
    if not cache_owner_present:
        errors.append("embedding cache has no subject deletion owner")
    if not public_rejected:
        errors.append("typed cognitive ACL can mint public visibility")
    if not wiki_receipt_schema_present:
        errors.append("Wiki lifecycle lacks typed subject deletion receipts")

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not errors,
        "acl_unknown": int(effect_matrix.get("acl_unknown") or 0)
        + int(acl_inventory.get("active_acl_lineage_gap") or 0)
        + int(acl_inventory.get("active_acl_authorization_gap") or 0),
        "default_public": 0 if public_rejected else 1,
        "cross_scope_leak": int(acl_runtime.get("cross_scope_leak") or 0),
        "pre_body_authorization_gap": int(
            pre_body_runtime.get("pre_body_authorization_gap") or 0
        ),
        "active_acl_lineage_gap": int(
            acl_inventory.get("active_acl_lineage_gap") or 0
        ),
        "active_acl_authorization_gap": int(
            acl_inventory.get("active_acl_authorization_gap") or 0
        ),
        "active_acl_inventory": acl_inventory,
        "required_object_type_denominator": {
            domain: list(object_types)
            for domain, object_types in _REQUIRED_OBJECT_TYPE_DENOMINATOR.items()
        },
        "missing_object_types": sorted(missing_object_types),
        "acl_decision_matrix": acl_runtime,
        "pre_body_authorization_matrix": pre_body_runtime,
        "verified_without_physical_effect": effect_matrix.get(
            "verified_without_physical_effect"
        ),
        "active_subject_rows_after_verified": effect_matrix.get(
            "active_subject_rows_after_verified"
        ),
        "derived_projection_gap": effect_matrix.get("derived_projection_gap"),
        "adapter_coverage": adapter_coverage,
        "physical_delete_owners": sorted(_PHYSICAL_DELETE_OWNERS),
        "tombstone_only_owners": sorted(_TOMBSTONE_ONLY_OWNERS),
        "unimplemented_domains": unimplemented_domains,
        "ann_index_subject_delete_owner": ann_owner_present,
        "ann_index_subject_delete_owner_kind": "wiki_lifecycle_required_consumer",
        "embedding_cache_subject_delete_owner": cache_owner_present,
        "wiki_subject_deletion_receipt_schema": wiki_receipt_schema_present,
        "required_wiki_projection_consumers": list(DEFAULT_REQUIRED_CONSUMERS),
        "effect_matrix": effect_matrix,
        "failure_probe_denominator": list(FAILURE_MODES),
        "failure_probes": failure_probes,
        "target_failure_probe": failure_probes.get(
            "wiki_consumer:wiki_search_index", {}
        ),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail when any COG-043 gap remains")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("COG-043 ACL/deletion propagation audit: " + ("PASS" if report["ok"] else "FAIL"))
        for error in report["errors"]:
            print("- " + error)
    return 0 if report["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
