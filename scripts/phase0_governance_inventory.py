"""Source, schema, audit, and migration inventories for governance assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from core.ops.durable_io import DurableIOError
from core.ops.durable_io import read_native_bytes
from scripts import run_full_score_gates
from scripts.phase0_governance_constants import (
    FINDING_OWNERS,
    HISTORICAL_BODY_HEADING,
    IMPORTED_CORPUS_END,
    IMPORTED_CORPUS_START,
    INDEPENDENT_DENOMINATOR_PATH,
    MIGRATION_PREFIXES,
    PHASE0_LEDGER_PATH,
    PHASE1_CLOSURE_BOUNDARIES,
    PHASE1_LEDGER_PATH,
    PHASE1_REVALIDATION_BOUNDARY_OVERRIDES,
    PHASE1_REVALIDATION_SEQUENCE,
    ROOT,
    ROOT_ORDER,
)


def _read_bytes(path: Path) -> bytes:
    try:
        return read_native_bytes(path)
    except (DurableIOError, OSError):
        raise OSError("governance_inventory_source_unavailable") from None


def _read_text(path: Path) -> str:
    return _read_bytes(path).decode("utf-8")


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


@dataclass(frozen=True)
class InventoryContext:
    root: Path
    independent_denominator_path: Path
    phase0_ledger_path: Path
    phase1_ledger_path: Path
    root_order: tuple[tuple[str, str], ...]
    finding_owners: Mapping[str, tuple[str, ...]]
    phase1_closure_boundaries: Mapping[str, Mapping[str, Any]]
    phase1_revalidation_boundary_overrides: Mapping[str, Mapping[str, Any]]
    phase1_revalidation_sequence: tuple[tuple[str, str], ...]


_ACTIVE_CONTEXT: ContextVar[InventoryContext | None] = ContextVar(
    "phase0_governance_inventory_context",
    default=None,
)


@contextmanager
def inventory_scope(context: InventoryContext) -> Iterator[None]:
    token = _ACTIVE_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def _context() -> InventoryContext:
    active = _ACTIVE_CONTEXT.get()
    if active is not None:
        return active
    return InventoryContext(
        root=ROOT,
        independent_denominator_path=INDEPENDENT_DENOMINATOR_PATH,
        phase0_ledger_path=PHASE0_LEDGER_PATH,
        phase1_ledger_path=PHASE1_LEDGER_PATH,
        root_order=tuple(ROOT_ORDER),
        finding_owners=dict(FINDING_OWNERS),
        phase1_closure_boundaries=dict(PHASE1_CLOSURE_BOUNDARIES),
        phase1_revalidation_boundary_overrides=dict(
            PHASE1_REVALIDATION_BOUNDARY_OVERRIDES
        ),
        phase1_revalidation_sequence=tuple(PHASE1_REVALIDATION_SEQUENCE),
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _independent_denominator() -> dict[str, Any]:
    payload = json.loads(_read_text(_context().independent_denominator_path))
    if not isinstance(payload, dict):
        raise ValueError("independent denominator must be an object")
    return payload


def _git_blob_bytes(commit: str, relative_path: str) -> bytes | None:
    root = _context().root
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{relative_path}"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode:
        return None
    return subprocess.check_output(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=root,
    )


def _section_sha256(path: Path, start: str, end: str) -> str:
    text = _read_text(path)
    start_offset = text.index(start)
    end_offset = text.index(end, start_offset + len(start))
    return hashlib.sha256(text[start_offset:end_offset].encode()).hexdigest()


def _has_unique_ordered_section_anchors(path: Path, anchors: tuple[str, ...]) -> bool:
    text = _read_text(path)
    offsets: list[int] = []
    for anchor in anchors:
        matches = list(re.finditer(rf"(?m)^{re.escape(anchor)}(?:\s|$)", text))
        if len(matches) != 1:
            return False
        offsets.append(matches[0].start())
    return offsets == sorted(offsets)


def _imported_root_definition_ids(path: Path) -> list[str]:
    text = _read_text(path)
    if text.count(IMPORTED_CORPUS_START) != 1 or text.count(IMPORTED_CORPUS_END) != 1:
        return []
    start = text.index(IMPORTED_CORPUS_START)
    end = text.index(IMPORTED_CORPUS_END, start + len(IMPORTED_CORPUS_START))
    if end <= start:
        return []
    return re.findall(r"(?m)^##### .*?\[(COG-\d{3})\].*$", text[start:end])


def _shift_markdown_headings(text: str, levels: int = 2) -> str:
    output: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^( {0,3})(`{3,}|~{3,})", line)
        if fence:
            token = fence.group(2)
            if fence_character is None:
                fence_character = token[0]
                fence_length = len(token)
            elif token[0] == fence_character and len(token) >= fence_length:
                fence_character = None
                fence_length = 0
            output.append(line)
            continue
        if fence_character is None:
            line = re.sub(
                r"^(#{1,4})(?=\s)",
                lambda match: "#" * (len(match.group(1)) + levels),
                line,
            )
        output.append(line)
    return "".join(output)


def _imported_contract_corpus(path: Path) -> str:
    text = _read_text(path)
    start = text.index(IMPORTED_CORPUS_START)
    content_start = text.index("-->", start) + len("-->")
    if text[content_start : content_start + 2] != "\n\n":
        raise ValueError("imported contract corpus header must end with one blank line")
    content_start += 2
    content_end = text.index("\n" + IMPORTED_CORPUS_END, content_start)
    return text[content_start:content_end]


def _historical_contract_body(path: Path) -> str:
    text = _read_text(path)
    if text.count(HISTORICAL_BODY_HEADING) != 1:
        raise ValueError("historical contract body heading cardinality mismatch")
    return text[text.index(HISTORICAL_BODY_HEADING) :]


def _imported_corpus_matches_historical(
    governing_path: Path,
    historical_path: Path,
) -> bool:
    try:
        imported = _imported_contract_corpus(governing_path)
        historical = _historical_contract_body(historical_path)
    except (OSError, UnicodeError, ValueError):
        return False
    return imported == _shift_markdown_headings(historical)


def _full_score_gate_ids() -> list[str]:
    args = argparse.Namespace(
        strict=True,
        real_api=True,
        only=None,
        skip=None,
        skip_slow=False,
        skip_tests=False,
        skip_e2e=False,
        skip_wiki=False,
        skip_readiness=False,
    )
    return [gate.gate_id for gate in run_full_score_gates._expected_gate_plan(args)]


def _root_entries() -> list[dict[str, Any]]:
    context = _context()
    root_order = context.root_order
    finding_owners = context.finding_owners
    direct = {
        root_id: sorted(
            finding_id for finding_id, owners in finding_owners.items() if root_id in owners
        )
        for root_id, _ in root_order
    }
    entries: list[dict[str, Any]] = []
    previous_by_phase: dict[str, str] = {}
    previous_global: str | None = None
    for index, (root_id, phase_order) in enumerate(root_order):
        phase = phase_order.split("-", 1)[0]
        prerequisites = (
            [previous_by_phase[phase]]
            if phase in previous_by_phase
            else [previous_global] if previous_global else []
        )
        previous_by_phase[phase] = root_id
        previous_global = root_id
        payload = {
            "root_id": root_id,
            "phase_order": phase_order,
            "prerequisites": prerequisites,
            "prerequisite_wps": (
                ["WP-COG-046-P0-DENOMINATOR-LOCK"] if root_id == "COG-045" else []
            ),
            "canonical_owner": f"governing:{root_id}",
            "direct_finding_ids": direct[root_id],
            "implementation_commit": (
                "848b6795c67b48f9e69c64cf55e09145bf3f8bd4"
                if root_id == "COG-025"
                else (
                    "92c832673059fe659269211d219a1af4caa59e70"
                    if root_id == "COG-040"
                    else (
                        "566b73ac4189553dee824d881cdd28c69206ce3b" if root_id == "COG-046" else None
                    )
                )
            ),
            "production_snapshot_ref": None,
            "invalidates": [],
            "challenger": "required",
            "next_allowed": (
                root_order[index + 1][0] if index + 1 < len(root_order) else None
            ),
        }
        payload["contract_hash"] = _hash(payload)
        entries.append(payload)
    return entries


PHASE1_REGISTERED_SCHEMA_OWNERS = frozenset(
    {
        "core/agent_kit/authorization.py",
        "core/agent_kit/runtime_receipts.py",
        "core/app/raw_search.py",
        "core/sync_framework/agent_source_metadata_deletion.py",
        "core/sync_framework/capture_schema.py",
        "core/sync_framework/native_raw_contract_ledger.py",
        "core/sync_framework/raw_event_identity_aliases.py",
        "core/sync_framework/raw_event_identity_schema.py",
        "core/sync_framework/raw_event_store.py",
        "core/sync_framework/sync_engine.py",
        "core/sync_framework/triggers.py",
    }
)


def _schema_inventory() -> list[dict[str, Any]]:
    root = _context().root
    discovery_pattern = re.compile(
        r"\b(?:CREATE\s+(?:(?:UNIQUE\s+)?INDEX|(?:VIRTUAL\s+)?TABLE|VIEW|TRIGGER)|"
        r"ALTER\s+TABLE|"
        r"DROP\s+(?:TABLE|INDEX|VIEW|TRIGGER))\b",
        re.IGNORECASE,
    )
    pattern = re.compile(
        r"\b(CREATE\s+(?:(?:UNIQUE\s+)?INDEX|(?:VIRTUAL\s+)?TABLE|VIEW|TRIGGER)|"
        r"ALTER\s+TABLE|"
        r"DROP\s+(?:TABLE|INDEX|VIEW|TRIGGER))\b"
        r"(?:\s+IF\s+(?:NOT\s+)?EXISTS)?\s+[`\"']?([A-Za-z_][A-Za-z0-9_]*)?",
        re.IGNORECASE,
    )
    canonical = {
        "core/kia/relation_evidence_schema.py",
        "core/cognitive/search_state_headers.py",
        "core/cognitive/stage_receipt_schema_upgrade.py",
        "core/cognitive/state_schema.py",
        "core/cognitive/state_schema_ddl.py",
        "core/kia/amphora.py",
        "core/sync_framework/raw_session_identity_reconciliation.py",
        "daemon/agent_sync_cursor.py",
        "core/wiki_projection_lifecycle.py",
    }
    canonical.update(PHASE1_REGISTERED_SCHEMA_OWNERS)
    entries: list[dict[str, Any]] = []
    for base in (root / "core", root / "scripts", root / "daemon"):
        for path in sorted(base.rglob("*.py")):
            source = _read_bytes(path)
            text = source.decode("utf-8")
            if not discovery_pattern.search(text):
                continue
            matches = pattern.findall(text)
            relative = str(path.relative_to(root))
            entries.append(
                {
                    "path": relative,
                    "ddl_operations": sorted(
                        {re.sub(r"\s+", " ", operation.upper()) for operation, _ in matches}
                    ),
                    "ddl_objects": sorted({name for _, name in matches if name}),
                    "source_sha256": hashlib.sha256(source).hexdigest(),
                    "owner_status": ("REGISTERED" if relative in canonical else "UNREGISTERED"),
                    "release_blocking": relative not in canonical,
                }
            )
    return entries


def _audit_artifact_inventory() -> list[dict[str, Any]]:
    root = _context().root
    entries: list[dict[str, Any]] = []
    for path in _audit_artifact_paths():
        text = _read_text(path)
        match = re.search(r'SCHEMA_VERSION\s*=\s*"([^"]+)"', text)
        entries.append(
            {
                "artifact_id": path.stem,
                "runner_path": str(path.relative_to(root)),
                "artifact_schema": match.group(1) if match else None,
                "validator_symbol": None,
                "validator_source_hash": None,
                "execution_modes": [],
                "required_population_policy": "UNREGISTERED",
                "validator_status": "UNREGISTERED",
                "release_blocking": True,
            }
        )
    return entries


def _audit_artifact_paths() -> list[Path]:
    root = _context().root
    paths = set((root / "scripts").glob("audit_*.py"))
    paths.add(root / "scripts" / "security_audit.py")
    return sorted(path for path in paths if _is_regular_file(path))


def _migration_paths() -> list[Path]:
    root = _context().root
    paths = {
        path
        for path in (root / "scripts").glob("*.py")
        if path.name.startswith(MIGRATION_PREFIXES) or "--apply" in _read_text(path)
    }
    paths.update((root / "core" / "migrations").rglob("*.py"))
    for path in (root / "core").rglob("*.py"):
        if (
            path.name in {"migrate.py", "migration.py"}
            or "migration" in path.stem
            or "migrate" in path.stem
        ):
            paths.add(path)
    return sorted(paths)


def _closure_evidence() -> dict[str, dict[str, Any]]:
    context = _context()
    ledger = json.loads(_read_text(context.phase0_ledger_path))
    specs = {
        "COG-025": (
            "phase0_reopen_20260724",
            "phase0_cog025_followup_repair_20260725",
        ),
        "COG-040": (
            "phase0_cog040_baseline_20260724",
            "phase0_cog040_followup_revalidation_20260725",
        ),
        "COG-046": (
            "phase0_cog046_denominator_lock_20260724",
            "phase0_cog046_followup_repair_20260725",
        ),
    }
    allowed_states = _independent_denominator().get("closure_states", {})
    allowed_boundary_hashes = _independent_denominator().get("closure_boundary_hashes", {})
    result: dict[str, dict[str, Any]] = {}
    for root_id, (closure_key, evidence_key) in specs.items():
        record = ledger.get(closure_key)
        evidence_record = ledger.get(evidence_key)
        if not isinstance(record, dict) or not isinstance(evidence_record, dict):
            continue
        selected = {
            "parent_root_id": record.get("parent_root_id"),
            "work_package": record.get("work_package"),
            "source_commit": record.get("source_commit"),
            "state": record.get("state"),
            "closure_boundary": record.get("closure_boundary"),
            "evidence_generation": evidence_key,
            "evidence_generation_hash": _hash(
                {
                    key: evidence_record.get(key)
                    for key in (
                        "record_type",
                        "root_id",
                        "work_package",
                        "supersedes_evidence_record",
                        "direct_path",
                        "baseline_contract",
                        "artifacts",
                        "evidence_epoch_contract",
                        "requirement_revalidation",
                        "portability_revalidation",
                        "residual_dispositions",
                        "verification",
                        "closure_boundary",
                    )
                }
            ),
        }
        state = record.get("state")
        if (
            state != allowed_states.get(root_id)
            or _hash(record.get("closure_boundary")) != allowed_boundary_hashes.get(root_id)
            or evidence_record.get("root_id") != root_id
        ):
            state = "INVALID_LEDGER_STATE"
        result[root_id] = {
            "state": state,
            "evidence_pointer": (
                "docs/acceptance/cognitive_remediation_phase_0_ledger.json#" + evidence_key
            ),
            "evidence_hash": _hash(selected),
        }
    phase1_ledger = json.loads(_read_text(context.phase1_ledger_path))
    for root_id, evidence_key in context.phase1_revalidation_sequence:
        evidence_record = phase1_ledger.get(evidence_key)
        if not isinstance(evidence_record, dict):
            continue
        boundary = evidence_record.get("closure_boundary")
        expected_boundary = context.phase1_revalidation_boundary_overrides.get(
            evidence_key,
            context.phase1_closure_boundaries.get(root_id),
        )
        expected_state = allowed_states.get(root_id)
        state = evidence_record.get("state")
        if (
            evidence_record.get("root_id") != root_id
            or state != expected_state
            or boundary != expected_boundary
            or _hash(boundary) != allowed_boundary_hashes.get(root_id)
        ):
            state = "INVALID_LEDGER_STATE"
        result[root_id] = {
            "state": state,
            "evidence_pointer": (
                "docs/acceptance/cognitive_remediation_phase_1_ledger.json#" + evidence_key
            ),
            "evidence_hash": _hash(
                {
                    "record_type": evidence_record.get("record_type"),
                    "root_id": evidence_record.get("root_id"),
                    "work_package": evidence_record.get("work_package"),
                    "supersedes_evidence_record": evidence_record.get("supersedes_evidence_record"),
                    "state": evidence_record.get("state"),
                    "requirement_revalidation": evidence_record.get("requirement_revalidation"),
                    "code_contract": evidence_record.get("code_contract"),
                    "verification": evidence_record.get("verification"),
                    "remaining_live_requirements": evidence_record.get(
                        "remaining_live_requirements"
                    ),
                    "closure_boundary": boundary,
                }
            ),
        }
    return result
