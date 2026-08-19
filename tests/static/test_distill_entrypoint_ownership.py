"""Release invariants for the single production distillation owner."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SOURCES = (
    "daemon/distill_service.py",
    "core/hephaestus_worker.py",
    "core/hephaestus/distill_backend.py",
    "core/hephaestus/distillation_extractor.py",
)


def _seed_audit_tree(target: Path) -> None:
    for relative in REQUIRED_SOURCES:
        source = REPO_ROOT / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def test_current_runtime_has_exactly_one_typed_distill_owner():
    from core.hephaestus.distill_entrypoint_audit import audit_distill_entrypoint

    report = audit_distill_entrypoint(REPO_ROOT)

    assert report.ok is True
    assert report.active_owner_count == 1
    assert report.active_owner_paths[0].startswith("daemon/distill_service.py:")


def test_second_active_queue_owner_is_release_blocking(tmp_path):
    from core.hephaestus.distill_entrypoint_audit import audit_distill_entrypoint

    _seed_audit_tree(tmp_path)
    second = tmp_path / "daemon" / "second_distill_service.py"
    second.write_text(
        "def run(worker):\n"
        "    return worker.process_all()\n",
        encoding="utf-8",
    )

    report = audit_distill_entrypoint(tmp_path)

    assert report.active_owner_count == 2
    assert any("owner count must be exactly 1" in error for error in report.errors)


def test_legacy_external_output_collector_is_release_blocking(tmp_path):
    from core.hephaestus.distill_entrypoint_audit import audit_distill_entrypoint

    _seed_audit_tree(tmp_path)
    worker = tmp_path / "core" / "hephaestus_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8")
        + "\n    def collect_completed(self):\n"
        + "        return 0\n",
        encoding="utf-8",
    )

    report = audit_distill_entrypoint(tmp_path)

    assert any(
        "legacy external-output worker method is forbidden: collect_completed" in error
        for error in report.errors
    )


def test_parsed_only_backend_port_is_release_blocking(tmp_path):
    from core.hephaestus.distill_entrypoint_audit import audit_distill_entrypoint

    _seed_audit_tree(tmp_path)
    backend = tmp_path / "core" / "hephaestus" / "distill_backend.py"
    backend.write_text(
        backend.read_text(encoding="utf-8").replace(
            "self._caller.call_with_evidence",
            "self._caller.call",
            1,
        ),
        encoding="utf-8",
    )

    report = audit_distill_entrypoint(tmp_path)

    assert any("typed call_with_evidence port" in error for error in report.errors)


def test_sync_owner_must_route_through_timeout_future_owner(tmp_path):
    from core.hephaestus.distill_entrypoint_audit import audit_distill_entrypoint

    _seed_audit_tree(tmp_path)
    worker = tmp_path / "core" / "hephaestus_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8").replace(
            "future, runner = self._submit_distillation_future(",
            "future, runner = self._missing_future_owner(",
            1,
        ),
        encoding="utf-8",
    )

    report = audit_distill_entrypoint(tmp_path)

    assert any(
        "synchronous owner must route exactly once to the future owner"
        in error
        for error in report.errors
    )


def test_future_owner_must_route_exactly_once_to_engine_owner(tmp_path):
    from core.hephaestus.distill_entrypoint_audit import audit_distill_entrypoint

    _seed_audit_tree(tmp_path)
    worker = tmp_path / "core" / "hephaestus_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8").replace(
            "self._run_distillation_engine(session_id, distill_task)",
            "self._missing_engine_owner(session_id, distill_task)",
            1,
        ),
        encoding="utf-8",
    )

    report = audit_distill_entrypoint(tmp_path)

    assert any(
        "future owner must route exactly once to the engine owner" in error
        for error in report.errors
    )


def test_engine_owner_must_have_exactly_one_worker_call_site(tmp_path):
    from core.hephaestus.distill_entrypoint_audit import audit_distill_entrypoint

    _seed_audit_tree(tmp_path)
    worker = tmp_path / "core" / "hephaestus_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8")
        + "\n    def _second_engine_owner(self, session_id, distill_task):\n"
        + "        return self._run_distillation_engine(session_id, distill_task)\n",
        encoding="utf-8",
    )

    report = audit_distill_entrypoint(tmp_path)

    assert any(
        "engine owner must be reachable through exactly one worker call site"
        in error
        for error in report.errors
    )
