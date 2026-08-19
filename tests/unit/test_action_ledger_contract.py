from dataclasses import replace
import sqlite3

import pytest

from core.system_contracts import (
    ActionLedger,
    audit_action_ledger_contract,
    make_action_record,
    make_quality_gate_observation,
)
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
)
from core.ops.action_ledger import (
    ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY,
    ACTION_LEDGER_MATERIAL_EXECUTOR,
    ACTION_LEDGER_MATERIAL_OWNER,
    action_ledger_material_action_input_hash,
    verify_action_ledger_diagnostic_row,
)
from tests.cognitive_decision_fixtures import material_action_authorization


def test_action_ledger_contract_is_strictly_valid():
    assert audit_action_ledger_contract(strict=True) == []


def test_action_ledger_records_and_reads_actions(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    record = make_quality_gate_observation(
        actor="test",
        target="distill:claim",
        evidence_refs=("core/system_contracts.py",),
        details={"command": "pytest"},
    )

    action_id = ledger.record_observation(record)
    rows = ledger.recent()

    assert rows[0]["action_id"] == action_id
    assert rows[0]["evidence_refs"] == ["core/system_contracts.py"]
    assert rows[0]["verification"]["command"] == "pytest"
    assert ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY in rows[0]["verification"]
    with sqlite3.connect(ledger.db_path) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute("SELECT * FROM action_ledger").fetchone()
    assert stored is not None
    assert verify_action_ledger_diagnostic_row(dict(stored)) is True


def test_action_ledger_exact_replay_is_idempotent_but_conflict_cannot_replace(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    record = make_quality_gate_observation(
        observation_id="obsact-immutable",
        actor="test",
        target="distill:claim",
        evidence_refs=("core/system_contracts.py",),
    )

    assert ledger.record_observation(record) == record.action_id
    assert ledger.record_observation(record) == record.action_id
    with pytest.raises(ValueError, match="immutable action record conflict"):
        ledger.record_observation(replace(record, target="distill:other-claim"))

    rows = ledger.recent()
    assert len(rows) == 1
    assert rows[0]["target"] == "distill:claim"

    with sqlite3.connect(ledger.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE action_ledger SET target='mutated' WHERE action_id='obsact-immutable'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM action_ledger WHERE action_id='obsact-immutable'")


def test_rollback_required_for_auto_heal_actions():
    record = make_action_record(
        actor="test",
        action_type="auto_heal",
        target="wiki:page",
        evidence_refs=("core/system_contracts.py",),
        rollback_ref="backup://page",
    )

    assert record.validate() == []


@pytest.mark.no_canonical_material_actions
def test_material_action_record_fails_closed_without_canonical_authorization(
    tmp_path,
):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    record = make_action_record(
        actor="test",
        action_type="auto_heal",
        target="wiki:page",
        evidence_refs=("core/system_contracts.py",),
        rollback_ref="backup://page",
    )

    with pytest.raises(PermissionError, match="material-action authorization"):
        ledger.record(record)


def test_action_ledger_redacts_only_private_and_credential_literals(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    credential_value = "credential-value-that-must-not-persist"
    record = make_quality_gate_observation(
        actor="test",
        target="user alice@example.com requested a normal architecture review",
        evidence_refs=("card 4111 1111 1111 1111", "ordinary-domain-evidence"),
        details={
            "api_key": credential_value,
            "password": "secret-password",
            "ordinary_content": "preserve this explanation exactly",
        },
        observation_id="obsact-private-redaction",
    )

    assert ledger.record_observation(record) == record.action_id
    assert ledger.record_observation(record) == record.action_id
    row = ledger.recent()[0]
    serialized = str(row)
    assert "alice@example.com" not in serialized
    assert "4111 1111 1111 1111" not in serialized
    assert credential_value not in serialized
    assert "secret-password" not in serialized
    assert row["verification"]["ordinary_content"] == "preserve this explanation exactly"


def test_generic_action_type_cannot_select_the_diagnostic_bypass(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    generic = make_action_record(
        actor="test",
        action_type="quality_gate",
        target="distill:caller-selected",
        evidence_refs=("test:caller-selected",),
    )

    with pytest.raises(PermissionError, match="material-action authorization"):
        ledger.record(generic)
    with pytest.raises(TypeError, match="ActionLedgerObservation"):
        ledger.record_observation(generic)


def test_typed_diagnostic_rejects_a_disguised_material_action(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    observation = make_quality_gate_observation(
        actor="test",
        target="distill:disguised",
        evidence_refs=("test:disguised",),
        details={"observed_action_type": "install_setup"},
    )

    with pytest.raises(ValueError, match="material-action aliases"):
        ledger.record_observation(observation)


def test_action_ledger_recovers_crash_after_effect_without_duplicate(
    tmp_path,
    monkeypatch,
):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    record = make_action_record(
        actor="test",
        action_type="auto_heal",
        target="wiki:crash-window",
        evidence_refs=("test:crash-window",),
        rollback_ref="backup://crash-window",
    )
    authorization = material_action_authorization(
        tmp_path,
        action_type=record.action_type,
        owner=ACTION_LEDGER_MATERIAL_OWNER,
        executor=ACTION_LEDGER_MATERIAL_EXECUTOR,
        target_ref=record.target,
        input_hash=action_ledger_material_action_input_hash(record),
        nonce="action-ledger-crash-recovery",
    )
    original = MaterialActionAuthorization.record_terminal
    calls = 0

    def crash_once(self, terminal):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after ActionLedger commit")
        return original(self, terminal)

    monkeypatch.setattr(
        MaterialActionAuthorization,
        "record_terminal",
        crash_once,
    )
    with pytest.raises(OSError, match="after ActionLedger commit"):
        ledger.record(record, material_action=authorization)
    assert (
        ledger.record(record, material_action=authorization)
        == authorization.permit.action_id
    )

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_ledger").fetchone()[0] == 1
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        receipt = conn.execute(
            "SELECT status, target_effect_id FROM cognitive_state_effect_receipts "
            "WHERE command_id=?",
            (authorization.permit.command_id,),
        ).fetchone()
    assert receipt == ("committed", authorization.permit.effect_id)


def test_internal_persistence_alias_rejects_forged_capability(tmp_path):
    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    record = make_action_record(
        actor="test",
        action_type="quality_gate",
        target="distill:forged-internal-call",
        evidence_refs=("test:forged-internal-call",),
    )
    persist = ledger._persist_action_ledger_record

    with pytest.raises(PermissionError, match="internal validated capability"):
        persist(
            record,
            action_id=record.action_id,
            primary_authorization=None,
            permit=None,
            diagnostic_observation=False,
            persistence_nonce=object(),
        )

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_ledger").fetchone()[0] == 0
