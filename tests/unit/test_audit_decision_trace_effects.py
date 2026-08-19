from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from core.cognitive.decision_trace import MaterialActionTerminal
from core.cognitive.material_effect_ledger import record_target_effect
from core.cognitive.material_effect_schema import initialize_material_effect_schema
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_schema import initialize_cognitive_state_schema
from scripts.audit_decision_trace_effects import (
    _audit_action_ledger_persistence_call_sites,
    _audit_dead_letter_supersessions,
    _audit_delegated_sink_contracts,
    _audit_material_effect_schema_owners,
    _audit_sink_contracts,
    _guard_matcher,
    _permit_dominance,
    _receipt_matches,
    audit_decision_trace_effects,
)
from scripts import run_local_gates
from tests.cognitive_decision_fixtures import material_action_authorization


def test_audit_is_read_only_for_missing_store(tmp_path: Path) -> None:
    state_db = tmp_path / "missing" / "producer_consumer_ledger.db"

    report = audit_decision_trace_effects(state_db=state_db, strict=True)

    assert report["ok"] is True
    assert report["live"]["status"] == "not_initialized"
    assert report["sink_audit"]["bypass_count"] == 0
    assert not state_db.parent.exists()


def test_sink_audit_preserves_direct_and_delegated_denominators() -> None:
    root = Path(__file__).resolve().parents[2]

    report = _audit_sink_contracts(root)

    assert report["failures"] == []
    assert report["denominator"] == 33
    assert report["direct_sink_count"] == 31
    assert report["delegated_sink_count"] == 2
    assert len(
        {(row["file"], row["function"]) for row in report["sinks"]}
    ) == report["direct_sink_count"]
    assert len(
        {(row["file"], row["function"]) for row in report["delegated_sinks"]}
    ) == report["delegated_sink_count"]
    assert {
        (row["function"], row["delegate"])
        for row in report["delegated_sinks"]
    } == {
        ("update_blindspot_profile", "save_persona_version"),
        ("record_persona_calibration", "save_persona_version"),
    }
    assert all(row["delegate_guarded"] for row in report["delegated_sinks"])


def test_delegated_sink_audit_rejects_dropped_handoff_and_direct_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "delegated.py"
    source.write_text(
        "def wrapper(material_action):\n"
        "    execute('UPDATE persona')\n"
        "    return guarded_sink()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.audit_decision_trace_effects.DELEGATED_SINK_CONTRACTS",
        (("delegated.py", "wrapper", "guarded_sink", "material_action"),),
    )

    report = _audit_delegated_sink_contracts(tmp_path)

    assert len(report["failures"]) == 1
    assert report["rows"] == [
        {
            "file": "delegated.py",
            "function": "wrapper",
            "delegate": "guarded_sink",
            "authorization_parameter": "material_action",
            "status": "unguarded",
            "line": 1,
            "delegate_call_count": 1,
            "direct_effect_lines": [2],
            "authorization_parameter_declared": True,
            "authorization_handoff": False,
        }
    ]


def test_delegated_sink_audit_rejects_global_authorization_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "delegated.py"
    source.write_text(
        "material_action = object()\n"
        "def wrapper():\n"
        "    return guarded_sink(material_action=material_action)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.audit_decision_trace_effects.DELEGATED_SINK_CONTRACTS",
        (("delegated.py", "wrapper", "guarded_sink", "material_action"),),
    )

    report = _audit_delegated_sink_contracts(tmp_path)

    assert len(report["failures"]) == 1
    assert report["rows"][0]["authorization_parameter_declared"] is False
    assert report["rows"][0]["authorization_handoff"] is True
    assert report["rows"][0]["status"] == "unguarded"


def test_material_effect_schema_has_one_exact_production_ddl_owner() -> None:
    root = Path(__file__).resolve().parents[2]

    report = _audit_material_effect_schema_owners(root)

    assert report["failures"] == []
    assert {row["file"] for row in report["observed_owners"]} == {
        "core/cognitive/material_effect_schema.py"
    }


def test_audit_detects_and_closes_pending_material_action(tmp_path: Path) -> None:
    authorization = material_action_authorization(
        tmp_path,
        action_type="audit_test_action",
        owner="audit_test",
        executor="audit_test_executor",
        target_ref="audit-target:1",
        input_hash="sha256:" + "d" * 64,
    )

    pending = audit_decision_trace_effects(
        state_db=tmp_path / "producer_consumer_ledger.db",
        strict=True,
    )
    assert pending["ok"] is False
    assert pending["metrics"]["decision_without_action_terminal"] == 1

    permit = authorization.permit
    authorization.record_terminal(
        MaterialActionTerminal(
            status="committed",
            target_effect_id=permit.effect_id,
            before_hash="sha256:" + "1" * 64,
            after_hash="sha256:" + "2" * 64,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                "target-after:sha256:" + "2" * 64,
                "target-oracle:audit-test:sha256:" + "2" * 64,
            ),
            created_at="2026-07-17T10:00:00+00:00",
        )
    )

    closed = audit_decision_trace_effects(
        state_db=tmp_path / "producer_consumer_ledger.db",
        strict=True,
    )
    assert closed["ok"] is True
    assert set(closed["metrics"].values()) == {0}


def test_audit_rejects_typed_snapshot_source_purpose_contract_drift(
    tmp_path: Path,
) -> None:
    authorization = material_action_authorization(
        tmp_path,
        action_type="audit_source_purpose",
        owner="audit_test",
        executor="audit_test_executor",
        target_ref="audit-target:source-purpose",
        input_hash="sha256:" + "d" * 64,
    )
    permit = authorization.permit
    authorization.record_terminal(
        MaterialActionTerminal(
            status="committed",
            target_effect_id=permit.effect_id,
            before_hash="sha256:" + "1" * 64,
            after_hash="sha256:" + "2" * 64,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                "target-after:sha256:" + "2" * 64,
                "target-oracle:audit-test:sha256:" + "2" * 64,
            ),
            created_at="2026-07-17T10:00:00+00:00",
        )
    )
    state_db = tmp_path / "producer_consumer_ledger.db"
    clean = audit_decision_trace_effects(state_db=state_db, strict=True)
    assert clean["ok"] is True
    assert (
        clean["metrics"]["decision_snapshot_source_purpose_contract_gap"]
        == 0
    )

    with sqlite3.connect(state_db) as conn:
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        decision_row = conn.execute(
            "SELECT payload_json FROM cognitive_state_revisions WHERE revision_id=?",
            (permit.decision_revision_id,),
        ).fetchone()
        assert decision_row is not None
        decision_payload = json.loads(str(decision_row[0]))
        snapshot_revision_id = str(decision_payload["snapshot_revision_id"])
        snapshot_row = conn.execute(
            "SELECT payload_json FROM cognitive_state_revisions WHERE revision_id=?",
            (snapshot_revision_id,),
        ).fetchone()
        assert snapshot_row is not None
        snapshot_payload = json.loads(str(snapshot_row[0]))
        snapshot_payload["source_completeness"]["contract"]["contract_hash"] = (
            "sha256:" + "f" * 64
        )
        without_snapshot_hash = dict(snapshot_payload)
        without_snapshot_hash.pop("snapshot_hash")
        snapshot_payload["snapshot_hash"] = sha256_json(without_snapshot_hash)
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=?, payload_hash=? "
            "WHERE revision_id=?",
            (
                json.dumps(
                    snapshot_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                sha256_json(snapshot_payload),
                snapshot_revision_id,
            ),
        )
        decision_payload["snapshot_hash"] = snapshot_payload["snapshot_hash"]
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=?, payload_hash=? "
            "WHERE revision_id=?",
            (
                json.dumps(
                    decision_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                sha256_json(decision_payload),
                permit.decision_revision_id,
            ),
        )
        conn.execute(
            "CREATE TRIGGER cognitive_state_revisions_no_update "
            "BEFORE UPDATE ON cognitive_state_revisions BEGIN "
            "SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable'); END"
        )

    drifted = audit_decision_trace_effects(state_db=state_db, strict=True)
    assert drifted["ok"] is False
    assert (
        drifted["metrics"]["decision_snapshot_source_purpose_contract_gap"]
        == 1
    )


def test_independent_audit_requires_exact_dead_letter_supersession() -> None:
    action = {
        "owner": "retry-owner",
        "executor": "retry-executor",
        "action_type": "retry-action",
        "target_ref": "retry-target:exact",
        "input_hash": "sha256:" + "d" * 64,
    }
    prior = {
        "revision_id": "decision-prior",
        "scope_type": "project",
        "scope_id": "mnemos",
        "created_at": "2026-07-17T09:00:00+00:00",
        "payload": {"action_specs": [dict(action)]},
    }
    current = {
        "revision_id": "decision-current",
        "scope_type": "project",
        "scope_id": "mnemos",
        "created_at": "2026-07-17T09:02:00+00:00",
        "payload": {
            "action_specs": [dict(action)],
            "supersedes_decision_revision_ids": ["decision-prior"],
        },
    }
    commands = {
        "command-prior": {
            "command_id": "command-prior",
            "revision_id": "decision-prior",
            "payload": dict(action),
        }
    }
    receipts = {
        "command-prior": [
            {
                "receipt_id": "receipt-prior",
                "status": "dead_letter",
                "created_at": "2026-07-17T09:01:00+00:00",
            }
        ]
    }

    failures: list[str] = []
    _audit_dead_letter_supersessions(
        current,
        decisions={"decision-prior": prior, "decision-current": current},
        commands=commands,
        receipts_by_command=receipts,
        failures=failures,
    )
    assert failures == []

    current["payload"]["supersedes_decision_revision_ids"] = []
    _audit_dead_letter_supersessions(
        current,
        decisions={"decision-prior": prior, "decision-current": current},
        commands=commands,
        receipts_by_command=receipts,
        failures=failures,
    )
    assert any("dead-letter supersession mismatch" in value for value in failures)


def test_strict_audit_rejects_target_journal_receipt_hash_drift(
    tmp_path: Path,
) -> None:
    authorization = material_action_authorization(
        tmp_path,
        action_type="policy_patch_propose",
        owner="policy_patch",
        executor="policy_patch_store",
        target_ref="policy-patch:audit-target",
        input_hash="sha256:" + "d" * 64,
    )
    permit = authorization.permit
    target_db = tmp_path / "policy_patches.db"
    before_hash = "sha256:" + "1" * 64
    after_hash = "sha256:" + "2" * 64
    observed_at = "2026-07-17T10:00:00+00:00"
    evidence_refs = (
        f"target-after:{after_hash}",
        f"target-journal:policy-patch:audit-target:{after_hash}",
    )
    with sqlite3.connect(target_db) as conn:
        initialize_material_effect_schema(conn)
        record_target_effect(
            conn,
            permit,
            status="committed",
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=evidence_refs,
            observed_at=observed_at,
        )
    authorization.record_terminal(
        MaterialActionTerminal(
            status="committed",
            target_effect_id=permit.effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                *evidence_refs,
            ),
            created_at=observed_at,
        )
    )
    backup_db = tmp_path / "backups" / "policy_patches.db.sqlite3"
    backup_db.parent.mkdir()
    shutil.copy2(target_db, backup_db)

    clean = audit_decision_trace_effects(
        state_db=tmp_path / "producer_consumer_ledger.db",
        database_dir=tmp_path,
        strict=True,
    )
    assert clean["ok"] is True
    assert clean["target_effect_audit"]["journal_rows"] == 1
    assert clean["target_effect_audit"]["journal_databases"] == [
        str(target_db.resolve())
    ]

    with sqlite3.connect(target_db) as conn:
        conn.execute(
            "UPDATE material_target_effects SET after_hash=? WHERE command_id=?",
            ("sha256:" + "3" * 64, permit.command_id),
        )

    drifted = audit_decision_trace_effects(
        state_db=tmp_path / "producer_consumer_ledger.db",
        database_dir=tmp_path,
        strict=True,
    )
    assert drifted["ok"] is False
    assert any(
        "target effect journal does not match terminal receipt" in error
        for error in drifted["errors"]
    )


def test_foreign_canonical_store_authorization_is_rejected(tmp_path: Path) -> None:
    authorization = material_action_authorization(
        tmp_path / "foreign",
        action_type="foreign_test",
        owner="foreign",
        executor="foreign_executor",
        target_ref="foreign-target:1",
        input_hash="sha256:" + "e" * 64,
    )

    with pytest.raises(PermissionError, match="foreign canonical store"):
        from core.cognitive.decision_trace import resolve_material_action_authorization

        resolve_material_action_authorization(
            authorization,
            owner="foreign",
            executor_id="foreign_executor",
            action_type="foreign_test",
            target_ref="foreign-target:1",
            input_hash="sha256:" + "e" * 64,
            expected_state_db=tmp_path / "local" / "producer_consumer_ledger.db",
        )


def test_audit_cli_is_machine_readable(tmp_path: Path) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    script = Path(__file__).resolve().parents[2] / "scripts" / "audit_decision_trace_effects.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--strict",
            "--json",
            "--state-db",
            str(state_db),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "mnemos.decision_trace_effect_audit.v1"
    assert payload["ok"] is True


def test_decision_trace_audit_is_required_by_all_gate_surfaces() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    gate_commands = {name: command for name, command in run_local_gates.GATES}
    assert gate_commands["decision trace effects"] == [
        "python",
        "scripts/audit_decision_trace_effects.py",
        "--strict",
        "--json",
    ]
    expected = "python3 scripts/audit_decision_trace_effects.py --strict --json"
    assert expected in (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "python scripts/audit_decision_trace_effects.py --strict --json" in (
        repo_root / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "source",
    (
        "def sink():\n    effect()\n    guard()\n",
        "def sink():\n    if False:\n        guard()\n    effect()\n",
        "def sink():\n    def unused():\n        guard()\n    effect()\n",
    ),
)
def test_static_dominance_rejects_post_hoc_dead_or_nested_guards(source: str) -> None:
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.FunctionDef)

    result = _permit_dominance(
        function,
        guard_call="guard",
        effect_calls=("effect",),
    )

    assert result.effect_count == 1
    assert result.violation_lines


@pytest.mark.parametrize(
    "source",
    (
        "def sink():\n    False and guard()\n    effect()\n",
        "def sink():\n    all(guard() for _ in ())\n    effect()\n",
        "def sink():\n    assert guard()\n    effect()\n",
    ),
)
def test_static_dominance_rejects_non_executing_guard_expressions(
    source: str,
) -> None:
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.FunctionDef)

    result = _permit_dominance(
        function,
        guard_call="guard",
        effect_calls=("effect",),
    )

    assert result.violation_lines


def test_static_dominance_rejects_unguarded_match_branch() -> None:
    function = ast.parse(
        "def sink(value):\n"
        "    match value:\n"
        "        case 1:\n"
        "            guard()\n"
        "        case _:\n"
        "            effect()\n"
    ).body[0]
    assert isinstance(function, ast.FunctionDef)

    result = _permit_dominance(
        function,
        guard_call="guard",
        effect_calls=("effect",),
    )

    assert result.effect_count == 1
    assert result.violation_lines


def test_static_dominance_rejects_match_case_guard_as_authorization() -> None:
    function = ast.parse(
        "def sink(value):\n"
        "    match value:\n"
        "        case _ if guard():\n"
        "            effect()\n"
    ).body[0]
    assert isinstance(function, ast.FunctionDef)

    result = _permit_dominance(
        function,
        guard_call="guard",
        effect_calls=("effect",),
    )

    assert result.effect_count == 1
    assert result.violation_lines


def test_static_dominance_rejects_finally_effect_after_early_return() -> None:
    function = ast.parse(
        "def sink(stop):\n"
        "    try:\n"
        "        if stop:\n"
        "            return\n"
        "        guard()\n"
        "    finally:\n"
        "        effect()\n"
    ).body[0]
    assert isinstance(function, ast.FunctionDef)

    result = _permit_dominance(
        function,
        guard_call="guard",
        effect_calls=("effect",),
    )

    assert result.effect_count == 1
    assert result.violation_lines


def test_static_dominance_rejects_guard_suppressed_before_later_effect() -> None:
    function = ast.parse(
        "def sink():\n"
        "    with suppress(Exception):\n"
        "        guard()\n"
        "    effect()\n"
    ).body[0]
    assert isinstance(function, ast.FunctionDef)

    result = _permit_dominance(
        function,
        guard_call="guard",
        effect_calls=("effect",),
    )

    assert result.effect_count == 1
    assert result.violation_lines


@pytest.mark.parametrize(
    "definition",
    (
        "    @effect()\n    def nested():\n        pass\n",
        "    def nested(value=effect()):\n        pass\n",
        "    class Nested(effect()):\n        pass\n",
    ),
)
def test_static_dominance_checks_definition_time_effects(
    definition: str,
) -> None:
    function = ast.parse("def sink():\n" + definition + "    guard()\n").body[0]
    assert isinstance(function, ast.FunctionDef)

    result = _permit_dominance(
        function,
        guard_call="guard",
        effect_calls=("effect",),
    )

    assert result.effect_count == 1
    assert result.violation_lines


@pytest.mark.parametrize(
    "fake_guard",
    (
        "def require_material_action():\n    return object()\n",
        "def sink():\n"
        "    def require_material_action():\n"
        "        return object()\n"
        "    require_material_action()\n"
        "    effect()\n",
    ),
)
def test_static_dominance_rejects_same_name_noncanonical_guard(
    fake_guard: str,
) -> None:
    if fake_guard.startswith("def sink"):
        source = (
            "from core.cognitive.decision_trace import require_material_action\n"
            + fake_guard
        )
    else:
        source = (
            "from core.cognitive.decision_trace import require_material_action\n"
            + fake_guard
            + "def sink():\n    require_material_action()\n    effect()\n"
        )
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "sink"
    )
    matcher, _error = _guard_matcher(
        tree,
        function,
        "require_material_action",
    )

    result = _permit_dominance(
        function,
        guard_call="require_material_action",
        effect_calls=("effect",),
        guard_matcher=matcher,
    )

    assert result.violation_lines


def test_static_dominance_rejects_conditional_local_guard_delegate() -> None:
    tree = ast.parse(
        "from core.cognitive.decision_trace import require_material_action\n"
        "def local_guard(enabled):\n"
        "    if enabled:\n"
        "        require_material_action()\n"
        "def sink(enabled):\n"
        "    local_guard(enabled)\n"
        "    effect()\n"
    )
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "sink"
    )
    matcher, error = _guard_matcher(tree, function, "local_guard")

    result = _permit_dominance(
        function,
        guard_call="local_guard",
        effect_calls=("effect",),
        guard_matcher=matcher,
    )

    assert "forbidden" in error
    assert result.violation_lines


def test_static_audit_rejects_unregistered_action_ledger_persistence_caller(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    target = tmp_path / "core" / "ops" / "action_ledger.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        (repo_root / "core" / "ops" / "action_ledger.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    bypass = tmp_path / "integrations" / "bypass.py"
    bypass.parent.mkdir(parents=True)
    bypass.write_text(
        "def bypass(ledger, record):\n"
        "    persist = ledger._persist_action_ledger_record\n"
        "    return persist(record)\n",
        encoding="utf-8",
    )

    report = _audit_action_ledger_persistence_call_sites(tmp_path)

    assert any(
        "unexpected ActionLedger persistence access" in failure
        for failure in report["failures"]
    )


def test_static_audit_rejects_direct_action_ledger_sql_bypass(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for relative in (
        "core/ops/action_ledger.py",
        "core/ops/action_ledger_schema.py",
        "scripts/audit_cognitive_state_store.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            (repo_root / relative).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    bypass = tmp_path / "integrations" / "sql_bypass.py"
    bypass.parent.mkdir(parents=True)
    bypass.write_text(
        "def bypass(conn):\n"
        "    conn.execute(\"INSERT INTO action_ledger(action_id) VALUES ('x')\")\n",
        encoding="utf-8",
    )

    report = _audit_action_ledger_persistence_call_sites(tmp_path)

    assert any(
        "unexpected ActionLedger direct SQL sites" in failure
        for failure in report["failures"]
    )


def test_independent_receipt_semantics_reject_fake_terminal_evidence() -> None:
    command = {
        "command_id": "command-1",
        "revision_id": "decision-1",
        "event_id": "event-1",
        "consumer_id": "consumer-1",
        "payload": {"effect_id": "effect-1"},
    }
    base = {
        "revision_id": "decision-1",
        "event_id": "event-1",
        "consumer_id": "consumer-1",
        "target_effect_id": "effect-1",
        "before_hash": "sha256:" + "1" * 64,
        "after_hash": "sha256:" + "2" * 64,
        "evidence_refs": [
            "material-command:command-1",
            "decision-revision:decision-1",
            "material-effect:effect-1",
            "target-after:sha256:" + "2" * 64,
            "target-oracle:effect-1:test",
        ],
        "consumption_metadata": {
            "terminal_reason_code": "",
            "retry_exhausted": False,
        },
        "status": "committed",
    }
    assert _receipt_matches(command, base) is True

    missing_oracle = dict(base)
    missing_oracle["evidence_refs"] = base["evidence_refs"][:-1]
    assert _receipt_matches(command, missing_oracle) is False

    failed_without_reason = dict(base)
    failed_without_reason.update(
        status="failed_terminal",
        after_hash=base["before_hash"],
        evidence_refs=[
            "material-command:command-1",
            "decision-revision:decision-1",
            "material-effect:effect-1",
            "attempted-effect:effect-1",
            "target-oracle:effect-1:unchanged",
        ],
    )
    assert _receipt_matches(command, failed_without_reason) is False


def test_external_material_row_without_quarantine_or_runtime_provenance_fails(
    tmp_path: Path,
) -> None:
    initialize_cognitive_state_schema(
        tmp_path / "producer_consumer_ledger.db"
    )
    with sqlite3.connect(tmp_path / "action_ledger.db") as conn:
        conn.execute(
            """
            CREATE TABLE action_ledger (
                action_id TEXT PRIMARY KEY,
                evidence_refs_json TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                quality_decision_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                target TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO action_ledger VALUES (?, ?, ?, ?, ?, ?)",
            (
                "uncovered-action",
                '["source:test"]',
                "{}",
                "",
                "2026-07-17T00:00:00+00:00",
                "wiki://uncovered",
            ),
        )
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY,
                trust_decision_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decision TEXT NOT NULL
            )
            """
        )
    with sqlite3.connect(tmp_path / "trusted_push.db") as conn:
        conn.execute(
            """
            CREATE TABLE formal_cognitive_mutations (
                event_id TEXT PRIMARY KEY,
                evidence_refs TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                target_ref TEXT NOT NULL
            )
            """
        )

    report = audit_decision_trace_effects(
        state_db=tmp_path / "producer_consumer_ledger.db",
        database_dir=tmp_path,
        strict=True,
    )

    assert report["ok"] is False
    assert report["metrics"]["action_without_decision"] == 1
    assert report["external_domains"]["uncovered_count"] == 1
