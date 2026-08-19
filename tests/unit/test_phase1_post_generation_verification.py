from __future__ import annotations

import copy

import pytest

from core.ops.durable_io import DurableIOError
from scripts import generate_phase1_baseline_execution_evidence as execution_evidence
from scripts import phase1_governance_data as phase1_data
from scripts import verify_phase1_post_generation as post_generation


def _hermetic_execution(node_ids: tuple[str, ...]) -> dict:
    return {
        "execution_id": "phase1-current-post-generation",
        "exit_code": 0,
        "outcomes": {
            "passed": list(node_ids),
            "failed": [],
            "error": [],
            "skipped": [],
            "xfail": [],
            "xpass": [],
        },
        "executed_node_count": len(node_ids),
        "hermetic_run": {
            "profile": "isolated",
            "environment_hash": "a" * 64,
            "outside_write_count": 0,
            "formal_state_diff": [],
            "credentials_inherited": False,
            "manifest_verified": True,
            "manifest_integrity_digest": "b" * 64,
        },
    }


def test_post_generation_nodes_are_separate_from_pre_generation_evidence() -> None:
    post_nodes = set(
        phase1_data.PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT[
            "ROOT-COG-045"
        ]
    )
    spec = next(
        item
        for item in phase1_data.PHASE1_ROOT_REQUIREMENT_SPECS
        if item["requirement_id"] == "ROOT-COG-045"
    )

    assert len(post_nodes) == 5
    assert post_nodes == set(spec["post_generation_node_ids"])
    assert post_nodes.isdisjoint(spec["node_ids"])


def test_governance_loader_rejects_post_generation_node_overlap() -> None:
    payload = copy.deepcopy(phase1_data._DATA)  # noqa: SLF001
    changed_node = payload["PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT"][
        "ROOT-COG-045"
    ][0]
    payload["PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT"] = {
        "ROOT-COG-045": (changed_node,)
    }

    with pytest.raises(
        RuntimeError,
        match="post-generation test node denominator is invalid",
    ):
        phase1_data._validate_payload(payload)  # noqa: SLF001


def test_post_generation_execution_rejects_noncredit_or_partial_results() -> None:
    nodes = ("tests/test_fake.py::test_one", "tests/test_fake.py::test_two")
    execution = _hermetic_execution(nodes)
    execution["outcomes"]["passed"].pop()
    execution["outcomes"]["skipped"].append(
        "tests/test_fake.py::test_unowned"
    )

    errors = post_generation._execution_errors(execution, nodes)  # noqa: SLF001

    assert "post-generation execution contains noncredit outcomes" in errors
    assert (
        "post-generation execution does not cover the exact denominator"
        in errors
    )


def test_post_generation_artifact_rejects_stale_generation_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = ("tests/test_fake.py::test_contract",)
    binding = {"record_id": "phase1-current", "record_sha256": "a" * 64}
    denominator = {"node_ids": list(nodes), "node_count": 1}
    snapshot = {"path_count": 1, "sha256": "b" * 64}
    monkeypatch.setattr(
        post_generation,
        "_current_generation_binding",
        lambda: dict(binding),
    )
    monkeypatch.setattr(
        post_generation,
        "_post_generation_denominator",
        lambda _binding: dict(denominator),
    )
    monkeypatch.setattr(
        post_generation.governance,
        "_phase1_execution_snapshot",
        lambda: dict(snapshot),
    )
    monkeypatch.setattr(
        post_generation.governance,
        "validate_assets",
        lambda **_kwargs: [],
    )
    payload = {
        "schema_version": post_generation.SCHEMA_VERSION,
        "ok": True,
        "generation_binding": {**binding, "record_sha256": "c" * 64},
        "denominator": denominator,
        "source_snapshot": snapshot,
        "execution": _hermetic_execution(nodes),
        "production_boundary": {
            "production_data_read_or_written": False,
            "production_mutation_performed": False,
            "full_quick_executed": False,
        },
        "release_eligible": False,
    }
    payload["evidence_hash"] = post_generation.governance._hash(payload)

    errors = post_generation.validate_artifact(payload)

    assert errors == ["post-generation generation binding is stale"]


def test_post_generation_build_runs_only_the_exact_current_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = ("tests/test_fake.py::test_contract",)
    binding = {"record_id": "phase1-current", "record_sha256": "a" * 64}
    denominator = {"node_ids": list(nodes), "node_count": 1}
    snapshot = {"path_count": 1, "sha256": "b" * 64}
    observed: dict[str, object] = {}
    monkeypatch.setattr(post_generation.sys, "platform", "darwin")
    monkeypatch.setattr(
        post_generation.governance,
        "validate_assets",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        post_generation,
        "_current_generation_binding",
        lambda: dict(binding),
    )
    monkeypatch.setattr(
        post_generation,
        "_post_generation_denominator",
        lambda _binding: dict(denominator),
    )
    monkeypatch.setattr(
        post_generation.governance,
        "_phase1_execution_snapshot",
        lambda: dict(snapshot),
    )

    def fake_run_nodes(
        cwd,
        selected,
        *,
        execution_id: str,
        snapshot_hash: str,
    ) -> dict:
        observed.update(
            {
                "cwd": cwd,
                "selected": selected,
                "execution_id": execution_id,
                "snapshot_hash": snapshot_hash,
            }
        )
        return _hermetic_execution(selected)

    monkeypatch.setattr(post_generation, "_run_nodes", fake_run_nodes)

    artifact = post_generation.build_artifact()

    assert observed == {
        "cwd": post_generation.governance.ROOT,
        "selected": nodes,
        "execution_id": "phase1-current-post-generation",
        "snapshot_hash": snapshot["sha256"],
    }
    assert artifact["ok"] is True
    assert artifact["release_eligible"] is False
    assert artifact["production_boundary"]["production_mutation_performed"] is False


@pytest.mark.parametrize(
    ("module", "expected"),
    (
        (
            execution_evidence,
            "phase1_execution_artifact_publish_failed:durable_directory_path_unsafe",
        ),
        (
            post_generation,
            "phase1_post_generation_publish_failed:durable_directory_path_unsafe",
        ),
    ),
)
def test_phase1_artifact_publish_preserves_safe_durable_error_code(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    expected: str,
) -> None:
    def reject_publish(*_args, **_kwargs) -> None:
        raise DurableIOError("durable_directory_path_unsafe")

    monkeypatch.setattr(module, "secure_atomic_write_bytes", reject_publish)

    with pytest.raises(RuntimeError, match=expected):
        module._publish_artifact(tmp_path / "artifact.json", {"ok": True})  # noqa: SLF001
