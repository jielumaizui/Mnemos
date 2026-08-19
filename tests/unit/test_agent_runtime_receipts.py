from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

import core.agent_kit.runtime_receipts as runtime_receipts_module
from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore


def test_runtime_receipt_schema_late_abort_restores_preimage_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateSchemaAbort(BaseException):
        pass

    database = tmp_path / "agent_authorization.db"
    original_connect = sqlite3.connect
    with original_connect(database) as connection:
        connection.execute(
            "CREATE TABLE preimage_sentinel (value TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO preimage_sentinel(value) VALUES ('unchanged')"
        )

    opened: list[sqlite3.Connection] = []

    class FailingConnection(sqlite3.Connection):
        create_count = 0

        def execute(self, sql: str, parameters=(), /):  # type: ignore[override]
            result = super().execute(sql, parameters)
            if "CREATE TABLE" in str(sql).upper():
                self.create_count += 1
                if self.create_count == 3:
                    raise LateSchemaAbort("sentinel runtime receipt schema failure")
            return result

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = FailingConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(runtime_receipts_module.sqlite3, "connect", connect)

    with pytest.raises(
        LateSchemaAbort,
        match="sentinel runtime receipt schema failure",
    ):
        AgentRuntimeReceiptStore(database)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
    with original_connect(database) as connection:
        objects = connection.execute(
            """
            SELECT type, name FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        rows = connection.execute(
            "SELECT value FROM preimage_sentinel"
        ).fetchall()
    assert objects == [("table", "preimage_sentinel")]
    assert rows == [("unchanged",)]


def _sample() -> dict:
    return {
        "schema_version": "mnemos.agent_runtime_probe.v1",
        "user_content": "mnemos-runtime-probe-user",
        "assistant_content": "mnemos-runtime-probe-assistant",
        "tool_calls": [
            {
                "id": "mnemos-runtime-probe-call",
                "name": "health_check",
                "arguments": {},
            }
        ],
        "tool_results": [
            {
                "tool_call_id": "mnemos-runtime-probe-call",
                "status": "ok",
            }
        ],
        "completeness": {
            "visible_text": "full",
            "tool_calls": "full",
            "tool_results": "full",
            "truncated": False,
        },
    }


@pytest.mark.parametrize(
    ("operation", "call", "code"),
    [
        (
            "health",
            lambda store: store.get_health_check("codex"),
            "agent_health_receipt_store_unavailable",
        ),
        (
            "runtime",
            lambda store: store.get_receipt("codex"),
            "agent_runtime_receipt_store_unavailable",
        ),
        (
            "source_capture",
            lambda store: store.get_source_capture_receipt("codex"),
            "agent_source_capture_receipt_store_unavailable",
        ),
    ],
)
def test_existing_receipt_store_failure_is_unavailable_not_missing(
    tmp_path,
    operation,
    call,
    code,
):
    from core.agent_kit.runtime_receipts import (
        AgentRuntimeReceiptStateError,
        AgentRuntimeReceiptStore,
    )

    db_path = tmp_path / "agent_authorization.db"
    db_path.mkdir()
    store = AgentRuntimeReceiptStore(db_path, initialize=False)

    with pytest.raises(AgentRuntimeReceiptStateError, match=code):
        call(store)


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (
            lambda store: store.get_health_check("codex"),
            "agent_health_receipt_store_unavailable",
        ),
        (
            lambda store: store.get_receipt("codex"),
            "agent_runtime_receipt_store_unavailable",
        ),
        (
            lambda store: store.get_source_capture_receipt("codex"),
            "agent_source_capture_receipt_store_unavailable",
        ),
    ],
)
def test_uninspectable_receipt_store_is_never_reported_missing(
    tmp_path,
    monkeypatch,
    call,
    code,
):
    from pathlib import Path

    from core.agent_kit.runtime_receipts import (
        AgentRuntimeReceiptStateError,
        AgentRuntimeReceiptStore,
    )

    db_path = tmp_path / "agent_authorization.db"
    original_lstat = Path.lstat

    def denied(path, *args, **kwargs):
        if path == db_path:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    store = AgentRuntimeReceiptStore(db_path, initialize=False)

    with pytest.raises(AgentRuntimeReceiptStateError, match=code):
        call(store)


def test_runtime_receipt_read_only_path_never_follows_a_leaf_symlink(
    tmp_path,
) -> None:
    from core.agent_kit.runtime_receipts import (
        AgentRuntimeReceiptStateError,
        AgentRuntimeReceiptStore,
    )

    target = tmp_path / "agent_authorization.real.db"
    AgentRuntimeReceiptStore(target)
    link = tmp_path / "agent_authorization.db"
    link.symlink_to(target)
    store = AgentRuntimeReceiptStore(link, initialize=False)

    with pytest.raises(
        AgentRuntimeReceiptStateError,
        match="agent_runtime_receipt_store_unavailable",
    ):
        store.get_receipt("codex")


def test_agent_report_labels_receipt_storage_failure_unavailable(monkeypatch):
    from core.agent_kit import report
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStateError

    monkeypatch.setattr(
        "core.agent_kit.runtime_receipts.AgentRuntimeReceiptStore.evaluate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgentRuntimeReceiptStateError(
                "agent_runtime_receipt_store_unavailable"
            )
        ),
    )
    monkeypatch.setattr(
        "core.agent_kit.runtime_receipts.AgentRuntimeReceiptStore.evaluate_source_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgentRuntimeReceiptStateError(
                "agent_source_capture_receipt_store_unavailable"
            )
        ),
    )

    assert report._runtime_receipt_evaluation("codex")["runtime_state"] == "unavailable"
    assert (
        report._source_capture_receipt_evaluation("codex")[
            "source_capture_state"
        ]
        == "unavailable"
    )


def test_agent_report_preserves_passive_discovery_failure_state(
    tmp_path,
    monkeypatch,
):
    from core.agent_kit import report
    from core.sync_framework.registry import SourceRegistry

    class _BrokenSource:
        name = "codex"
        data_dir = tmp_path

        @staticmethod
        def completeness_capabilities():
            return {"source_fidelity": "full"}

        @staticmethod
        def discover_sessions():
            raise OSError("unavailable")

    monkeypatch.setattr(
        SourceRegistry,
        "list_builtin_agent_names",
        classmethod(lambda _cls: ["codex"]),
    )
    monkeypatch.setattr(
        SourceRegistry,
        "list_registered",
        classmethod(lambda _cls: []),
    )
    monkeypatch.setattr(
        SourceRegistry,
        "get_builtin_source_class",
        classmethod(lambda _cls, _name: _BrokenSource),
    )

    details = report._passive_source_details(
        "codex",
        probe_filesystem=True,
    )

    assert details["path_detected"] is True
    assert details["state"] == "unavailable"
    assert details["error_code"] == "passive_source_discovery_failed"


def test_agent_report_preserves_active_diagnostics_failure_state(monkeypatch):
    from core.agent_kit import report
    from core import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "_isolated_default_agent_status_providers",
        lambda: [
            type(
                "_BrokenProvider",
                (),
                {
                    "list_agent_statuses": lambda _self: (
                        _ for _ in ()
                    ).throw(OSError("active diagnostics unavailable"))
                },
            )()
        ],
    )
    monkeypatch.setattr(
        report,
        "agent_install_evidence",
        lambda _name: (True, "/fake/codex"),
    )
    monkeypatch.setattr(
        report,
        "_passive_source_details",
        lambda _agent, **_kwargs: {
            "registered": True,
            "detected": True,
            "state": "available",
            "path_detected": True,
            "data_dir": "/fake/codex",
            "capabilities": {"source_fidelity": "full"},
        },
    )

    result = report.build_agent_kit_report(
        ["codex"],
        load_default_providers=False,
        isolated_default_providers=True,
    )
    agent = result.agents[0]

    assert agent.active_runtime_state == "unavailable"
    assert (
        agent.active_runtime_error_code
        == "active_diagnostics_provider_probe_failed"
    )
    assert any(
        "active runtime diagnostics unavailable" in gap
        for gap in agent.full_power_gaps
    )


def test_agent_diagnostics_preserves_default_provider_load_failure_state(monkeypatch):
    from core import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "_isolated_default_agent_status_providers",
        lambda: (_ for _ in ()).throw(
            diagnostics.AgentDiagnosticsUnavailableError(
                "active_diagnostics_provider_load_failed"
            )
        ),
    )

    statuses = diagnostics.ConnectionDiagnostics.check_agents(
        load_default_providers=False,
        isolated_default_providers=True,
    )
    codex = next(status for status in statuses if status.name == "codex")

    assert codex.active_runtime_state == "unavailable"
    assert (
        codex.active_runtime_error_code
        == "active_diagnostics_provider_load_failed"
    )


def test_agent_report_preserves_workflow_tool_inventory_failure(monkeypatch):
    from core.agent_kit import report

    monkeypatch.setattr(
        report,
        "_safe_mcp_tool_names",
        lambda: (_ for _ in ()).throw(
            report.AgentKitInventoryUnavailableError(
                "agent_kit_workflow_tool_inventory_unavailable"
            )
        ),
    )
    monkeypatch.setattr(report, "_safe_active_adapter_names", lambda: set())
    monkeypatch.setattr(report, "_active_status_by_agent", lambda _load: {})
    monkeypatch.setattr(report, "_passive_registered_names", lambda: set())
    monkeypatch.setattr(
        report,
        "_passive_source_details",
        lambda _agent, **_kwargs: {"state": "unprobed"},
    )
    monkeypatch.setattr(
        report,
        "agent_install_evidence",
        lambda _name: (False, None),
    )

    result = report.build_agent_kit_report(
        ["codex"],
        probe_filesystem=False,
        load_default_providers=False,
    )

    assert result.workflow_tool_state == "unavailable"
    assert (
        result.workflow_tool_error_code
        == "agent_kit_workflow_tool_inventory_unavailable"
    )
    assert result.workflow_contract_ok is False


def test_agent_report_preserves_active_adapter_registry_failure(monkeypatch):
    from core.agent_kit import report
    from core.agent_kit.protocol import required_workflow_tool_names

    monkeypatch.setattr(
        report,
        "_safe_mcp_tool_names",
        lambda: set(required_workflow_tool_names()),
    )
    monkeypatch.setattr(
        report,
        "_safe_active_adapter_names",
        lambda: (_ for _ in ()).throw(
            report.AgentKitInventoryUnavailableError(
                "agent_kit_active_adapter_registry_unavailable"
            )
        ),
    )
    monkeypatch.setattr(report, "_active_status_by_agent", lambda _load: {})
    monkeypatch.setattr(report, "_passive_registered_names", lambda: set())
    monkeypatch.setattr(
        report,
        "_passive_source_details",
        lambda _agent, **_kwargs: {"state": "unprobed"},
    )
    monkeypatch.setattr(
        report,
        "agent_install_evidence",
        lambda _name: (True, "/fake/claude"),
    )

    result = report.build_agent_kit_report(
        ["claude"],
        probe_filesystem=False,
        load_default_providers=False,
    )
    agent = result.agents[0]

    assert result.active_adapter_registry_state == "unavailable"
    assert (
        result.active_adapter_registry_error_code
        == "agent_kit_active_adapter_registry_unavailable"
    )
    assert agent.active_runtime_state == "unavailable"
    assert (
        agent.active_runtime_error_code
        == "agent_kit_active_adapter_registry_unavailable"
    )


def test_workflow_tool_inventory_rejects_malformed_registry(monkeypatch):
    from core.agent_kit import report
    from integrations.agora_tools import schema

    monkeypatch.setattr(
        schema,
        "list_tools",
        lambda _handler: {"tools": [{"name": ""}]},
    )

    with pytest.raises(
        report.AgentKitInventoryUnavailableError,
        match="agent_kit_workflow_tool_inventory_unavailable",
    ):
        report._safe_mcp_tool_names()


def test_active_adapter_inventory_rejects_malformed_registry(monkeypatch):
    from core.agent_kit import report
    from integrations.olympus import AgentRegistry

    monkeypatch.setattr(
        AgentRegistry,
        "_ensure_adapters_loaded",
        classmethod(lambda _cls: None),
    )
    monkeypatch.setattr(AgentRegistry, "_adapters", None)

    with pytest.raises(
        report.AgentKitInventoryUnavailableError,
        match="agent_kit_active_adapter_registry_unavailable",
    ):
        report._safe_active_adapter_names()


def test_runtime_probe_requires_authorization_and_does_not_store_sample_content(tmp_path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("codex", "probe_ok")
    store = AgentRuntimeReceiptStore(db_path)

    denied = store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )

    assert denied["success"] is False
    assert denied["authorization_state"] == "probe_ok"
    assert denied["runtime_state"] == "authorization_denied"

    AgentAuthorizationStore(db_path).set_state("codex", "user_authorized")
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    accepted = store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )

    assert accepted["success"] is True
    assert accepted["runtime_state"] == "verified"
    assert accepted["runtime_receipt_at"]
    assert accepted["sample_completeness"] == _sample()["completeness"]
    assert "user_content" not in store.get_receipt("codex")
    assert "assistant_content" not in store.get_receipt("codex")


def test_runtime_probe_receipt_binds_a_content_free_canary_hash(tmp_path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import (
        AgentRuntimeReceiptStore,
        runtime_probe_canary_hash,
    )
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("codex", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)

    accepted = store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )

    expected_hash = runtime_probe_canary_hash(
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )
    assert accepted["runtime_canary_hash"] == expected_hash
    assert len(expected_hash) == 64
    stored = store.get_receipt("codex")
    assert stored["runtime_canary_hash"] == expected_hash
    assert "user_content" not in stored
    assert "assistant_content" not in stored


def test_runtime_probe_rejects_current_receipt_without_canary_hash(tmp_path):
    import json
    import sqlite3

    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("codex", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    assert store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )["success"]
    with sqlite3.connect(db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT sample_completeness_json FROM agent_runtime_receipts WHERE agent='codex'"
            ).fetchone()[0]
        )
        payload.pop("runtime_canary_hash")
        conn.execute(
            """
            UPDATE agent_runtime_receipts
            SET sample_completeness_json=?
            WHERE agent='codex'
            """,
            (json.dumps(payload, sort_keys=True),),
        )

    evaluated = store.evaluate("codex")

    assert evaluated["success"] is False
    assert evaluated["runtime_state"] == "runtime_canary_hash_missing"


def test_runtime_probe_rejects_well_formed_but_wrong_canary_hash(tmp_path):
    import json
    import sqlite3

    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("codex", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    assert store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )["success"]
    with sqlite3.connect(db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT sample_completeness_json FROM agent_runtime_receipts WHERE agent='codex'"
            ).fetchone()[0]
        )
        payload["runtime_canary_hash"] = "f" * 64
        conn.execute(
            """
            UPDATE agent_runtime_receipts
            SET sample_completeness_json=?
            WHERE agent='codex'
            """,
            (json.dumps(payload, sort_keys=True),),
        )

    evaluated = store.evaluate("codex")

    assert evaluated["success"] is False
    assert evaluated["runtime_state"] == "runtime_canary_hash_mismatch"


def test_runtime_probe_rejects_legacy_payload_even_with_injected_canary_hash(tmp_path):
    import json
    import sqlite3

    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("codex", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH)
    assert store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )["success"]
    with sqlite3.connect(db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT sample_completeness_json FROM agent_runtime_receipts WHERE agent='codex'"
            ).fetchone()[0]
        )
        payload["schema_version"] = "mnemos.agent_runtime_receipt_payload.v2"
        conn.execute(
            """
            UPDATE agent_runtime_receipts
            SET sample_completeness_json=?
            WHERE agent='codex'
            """,
            (json.dumps(payload, sort_keys=True),),
        )

    evaluated = store.evaluate("codex")

    assert evaluated["success"] is False
    assert evaluated["runtime_state"] == "runtime_receipt_payload_unsupported"


def test_runtime_probe_requires_a_recent_real_health_roundtrip(tmp_path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("codex", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)

    missing = store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )
    assert missing["success"] is False
    assert missing["runtime_state"] == "health_roundtrip_missing"

    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    store.record_health_check("codex", CANONICAL_HEALTH_CHECK_IDS_HASH, now=old)
    stale = store.record_probe(
        "codex",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )
    assert stale["success"] is False
    assert stale["runtime_state"] == "health_roundtrip_stale"


def test_runtime_probe_accepts_every_supported_host_with_the_same_safe_contract(tmp_path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.protocol import TARGET_AGENT_NAMES
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    authorization = AgentAuthorizationStore(db_path)
    store = AgentRuntimeReceiptStore(db_path)

    for agent in TARGET_AGENT_NAMES:
        authorization.set_state(agent, "user_authorized")
        store.record_health_check(agent, CANONICAL_HEALTH_CHECK_IDS_HASH)
        result = store.record_probe(
            agent,
            health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
            sample=_sample(),
        )
        assert result["success"] is True, agent
        assert result["runtime_state"] == "verified", agent


def test_runtime_probe_rejects_malformed_sample_and_invalidates_previous_receipt(tmp_path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("kiro", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)
    store.record_health_check("kiro", CANONICAL_HEALTH_CHECK_IDS_HASH)
    assert store.record_probe(
        "kiro",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
    )["success"]

    malformed = _sample()
    malformed["tool_results"] = []
    rejected = store.record_probe(
        "kiro",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=malformed,
    )

    assert rejected["success"] is False
    assert rejected["runtime_state"] == "malformed_sample"
    assert store.evaluate("kiro")["runtime_state"] == "malformed_sample"


def test_runtime_receipt_fails_closed_when_stale_or_health_check_set_changed(tmp_path):
    from core.agent_kit.authorization import AgentAuthorizationStore
    from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    db_path = tmp_path / "agent_authorization.db"
    AgentAuthorizationStore(db_path).set_state("claude", "user_authorized")
    store = AgentRuntimeReceiptStore(db_path)
    old_now = datetime.now(timezone.utc) - timedelta(days=2)
    store.record_health_check("claude", CANONICAL_HEALTH_CHECK_IDS_HASH, now=old_now)
    store.record_probe(
        "claude",
        health_check_ids_hash=CANONICAL_HEALTH_CHECK_IDS_HASH,
        sample=_sample(),
        now=old_now,
    )
    assert store.evaluate("claude", max_age_seconds=3600)["runtime_state"] == "stale"

    store.record_health_check("claude", "old-daemon-check-set")
    store.record_probe(
        "claude",
        health_check_ids_hash="old-daemon-check-set",
        sample=_sample(),
    )
    assert store.evaluate("claude")["runtime_state"] == "health_check_set_mismatch"


def test_agent_report_separates_static_conformance_from_runtime_full_power(monkeypatch):
    from core.diagnostics import AgentStatus
    from core.agent_kit.report import build_agent_kit_report

    monkeypatch.setattr(
        "core.agent_kit.report.agent_install_evidence",
        lambda _name: (True, "/fake/codex"),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._active_status_by_agent",
        lambda _load: {
            "codex": AgentStatus(
                name="codex",
                available=True,
                mcp_configured=True,
                policy_installed=True,
                active_ready=True,
            )
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._passive_source_details",
        lambda _agent, **_kwargs: {
            "registered": True,
            "detected": True,
            "data_dir": "/fake/codex",
            "capabilities": {
                "visible_text": True,
                "tool_calls": True,
                "tool_results": True,
                "reasoning": True,
                "attachments": True,
                "source_fidelity": True,
            },
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._runtime_receipt_evaluation",
        lambda _agent: {
            "runtime_state": "missing",
            "runtime_receipt_at": "",
            "sample_completeness": {},
            "health_check_ids_hash": "",
            "error": "runtime capability receipt missing",
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._source_capture_receipt_evaluation",
        lambda _agent: {
            "source_capture_state": "missing",
            "source_capture_receipt_at": "",
            "native_source_snapshot_hash": "",
            "capture_completeness": {},
            "error": "source capture receipt missing",
        },
    )

    report = build_agent_kit_report(["codex"], load_default_providers=False)
    agent = report.agents[0]

    assert agent.conformance_ok is True
    assert report.conformance_ok is True
    assert agent.full_power is False
    assert agent.runtime_state == "missing"
    assert agent.source_capture_state == "missing"
    assert report.to_dict()["agents"][0]["verification_layers"] == {
        "installed": True,
        "path_detected": True,
        "discovery_covered": False,
        "content_parsed": False,
        "raw_committed": False,
        "runtime_verified": False,
        "runtime_canary_verified": False,
    }
    assert report.full_power_ok is False
    assert report.to_dict()["agents"][0]["conformance_ok"] is True


def test_agent_status_requires_independent_raw_canary_for_full_power():
    from core.agent_kit.report import AgentKitAgentStatus

    status = AgentKitAgentStatus(
        name="codex",
        active_entrypoint="mcp_only",
        installed=True,
        content_access_authorized=True,
        runtime_state="verified",
        source_capture_state="verified",
        discovery_covered=True,
        content_parsed=True,
        raw_committed=True,
        runtime_canary_verified=False,
    )

    assert status.full_power is False
    status.runtime_canary_verified = True
    assert status.full_power is True


def test_single_verified_host_is_scoped_green_but_never_global_eight_host_green():
    from core.agent_kit.report import AgentKitAgentStatus, AgentKitReport
    from core.agent_kit.protocol import TARGET_AGENT_NAMES

    status = AgentKitAgentStatus(
        name="codex",
        active_entrypoint="mcp_only",
        installed=True,
        active_ready=True,
        mcp_configured=True,
        policy_installed=True,
        passive_source_registered=True,
        passive_source_detected=True,
        content_access_authorized=True,
        runtime_state="verified",
        source_capture_state="verified",
        discovery_covered=True,
        content_parsed=True,
        raw_committed=True,
        runtime_canary_verified=True,
    )
    report = AgentKitReport(
        protocol_version="agent-kit-v2",
        target_agents=["codex"],
        workflows=[],
        agents=[status],
        missing_workflow_tools=[],
    )

    assert report.selected_target_full_power_ok is True
    assert report.selected_runtime_unverified_agents == []
    assert report.full_power_ok is False
    assert report.target_agent_coverage_ok is False
    assert report.runtime_unverified_agents == list(TARGET_AGENT_NAMES[1:])


def test_agent_report_requires_and_accepts_current_authorized_runtime_receipt(monkeypatch):
    from core.diagnostics import AgentStatus
    from core.agent_kit.protocol import TARGET_AGENT_NAMES
    from core.agent_kit.report import build_agent_kit_report
    from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

    monkeypatch.setattr(
        "core.agent_kit.report.agent_install_evidence",
        lambda _name: (True, "/fake/codex"),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._active_status_by_agent",
        lambda _load: {
            name: AgentStatus(
                name=name,
                available=True,
                hooks_installed=True,
                mcp_configured=True,
                policy_installed=True,
                active_ready=True,
            )
            for name in TARGET_AGENT_NAMES
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._passive_source_details",
        lambda _agent, **_kwargs: {
            "registered": True,
            "detected": True,
            "data_dir": "/fake/codex",
            "capabilities": {
                "visible_text": True,
                "tool_calls": True,
                "tool_results": True,
                "reasoning": True,
                "attachments": True,
                "source_fidelity": True,
            },
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._authorization_state",
        lambda _agent, **_kwargs: ("user_authorized", True),
    )
    monkeypatch.setattr(
        "core.agent_kit.report._runtime_receipt_evaluation",
        lambda _agent: {
            "runtime_state": "verified",
            "runtime_receipt_at": "2026-07-11T00:00:00+00:00",
            "sample_completeness": _sample()["completeness"],
            "health_check_ids_hash": CANONICAL_HEALTH_CHECK_IDS_HASH,
            "runtime_canary_hash": "a" * 64,
            "error": "",
        },
    )
    monkeypatch.setattr(
        "core.agent_kit.report._source_capture_receipt_evaluation",
        lambda _agent: {
            "source_capture_state": "verified",
            "source_capture_receipt_at": "2026-07-11T00:00:00+00:00",
            "native_source_snapshot_hash": "a" * 64,
            "capture_completeness": {
                "discovery_covered": True,
                "content_parsed": True,
                "raw_committed": True,
                "runtime_canary_verified": True,
                "runtime_canary_hash": "a" * 64,
            },
            "error": "",
        },
    )

    report = build_agent_kit_report(load_default_providers=False)

    assert all(agent.full_power for agent in report.agents)
    assert report.full_power_agents == list(TARGET_AGENT_NAMES)
    assert report.target_agent_coverage_ok is True
    assert report.full_power_ok is True
