from pathlib import Path
import json

from core.trust.static_scan import REQUIRED_GUARDS, scan_direct_writes, scan_trusted_push_guards


def test_new_write_paths_require_trusted_push_guard():
    assert scan_trusted_push_guards() == []


def test_scan_reports_zero_known_bypass_debt_in_current_repo():
    report = scan_direct_writes()

    assert report["schema_version"] == "mnemos.trusted_push_static_scan.v4"
    assert report["counts"].get("known_bypass", 0) == 0
    assert report["unknown_count"] == 0
    assert report["registry_stale_count"] == 0


def test_unclassified_direct_write_fails(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "new_writer.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "def write(path: Path):\n"
        "    path.write_text('formal', encoding='utf-8')\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any("new_writer.py:4 unclassified direct write" in issue for issue in issues)


def test_secure_durable_helper_sink_is_discovered_and_requires_classification(
    tmp_path,
):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "secure_helper_writer.py"
    writer.write_text(
        "from core.ops.durable_io import secure_publish_immutable_text\n\n"
        "def write(root):\n"
        "    secure_publish_immutable_text(root, 'artifact.md', 'formal')\n",
        encoding="utf-8",
    )
    remover = tmp_path / "core" / "secure_helper_remover.py"
    remover.write_text(
        "from core.ops.durable_io import secure_remove_regular_file\n\n"
        "def remove(root):\n"
        "    secure_remove_regular_file(root, 'artifact.md')\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any(
        "secure_helper_writer.py:4 unclassified direct write" in issue
        and "secure_publish_immutable_text" in issue
        for issue in issues
    )
    assert any(
        "secure_helper_remover.py:4 unclassified direct write" in issue
        and "secure_remove_regular_file" in issue
        for issue in issues
    )


def test_inline_nonformal_write_classification_passes(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "artifact_writer.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "def write(path: Path):\n"
        "    # trusted-scan: artifact owner=test target=run_artifact expires=never\n"
        "    path.write_text('artifact', encoding='utf-8')\n",
        encoding="utf-8",
    )

    assert scan_trusted_push_guards(tmp_path) == []


def test_known_bypass_category_fails(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "legacy_writer.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "def write(path: Path):\n"
        "    # trusted-scan: known_bypass owner=test target=formal_markdown expires=2099-01-01\n"
        "    path.write_text('formal', encoding='utf-8')\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any("unsupported direct write category known_bypass" in issue for issue in issues)


def test_allowlisted_file_cannot_hide_new_write_bytes_sink(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "hephaestus" / "distillation_engine.py"
    writer.write_text(
        writer.read_text(encoding="utf-8")
        + "\n\ndef bypass(path: Path):\n"
        + "    path.write_bytes(b'formal')\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any("distillation_engine.py" in issue and "write_bytes" in issue for issue in issues)


def test_projection_lifecycle_only_allows_exact_receipt_guarded_sinks(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "wiki_derived_projection.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "class DerivedProjectionMutationAuthorization:\n"
        "    def assert_upsert(self, path, content): ...\n\n"
        "class DerivedProjectionLifecycle:\n"
        "    @staticmethod\n"
        "    def _atomic_publish(authorization: DerivedProjectionMutationAuthorization, path: Path, content: str):\n"
        "        authorization.assert_upsert(path, content)\n"
        "        atomic_write_text(path, content, encoding='utf-8')\n\n"
        "    @staticmethod\n"
        "    def bypass(path: Path):\n"
        "        path.write_text('formal', encoding='utf-8')\n",
        encoding="utf-8",
    )

    report = scan_direct_writes(tmp_path)
    projection_sites = [
        site for site in report["sites"] if site.rel_path == "core/wiki_derived_projection.py"
    ]

    assert projection_sites[0].category == "guarded_projection_lifecycle"
    assert projection_sites[0].guard_dominates is True
    assert projection_sites[1].category == "unclassified"


def test_projection_sink_without_exact_typed_validator_fails_closed(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "wiki_derived_projection.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "class DerivedProjectionLifecycle:\n"
        "    @staticmethod\n"
        "    def _atomic_publish(authorization, path: Path, content: str):\n"
        "        atomic_write_text(path, content, encoding='utf-8')\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any(
        "wiki_derived_projection.py" in issue and "unclassified direct write" in issue
        for issue in issues
    )


def test_shutil_move_sink_is_discovered_and_fails_closed(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "move_writer.py"
    writer.write_text(
        "import shutil\n\n"
        "def bypass(source, target):\n"
        "    shutil.move(source, target)\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any("move_writer.py:4" in issue and "shutil.move" in issue for issue in issues)


def test_file_marker_does_not_prove_guard_dominance(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "conditional_writer.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "def write(path: Path, approved: bool):\n"
        "    if approved:\n"
        "        trusted = submit_candidate()\n"
        "        if trusted.intercepted:\n"
        "            return\n"
        "    path.write_text('formal', encoding='utf-8')\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any(
        "conditional_writer.py:8" in issue and "guard_dominates=false" in issue
        for issue in issues
    )


def test_inline_classification_requires_owner_target_and_expiry(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "weak_marker.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "def write(path: Path):\n"
        "    # trusted-scan: artifact vague marker\n"
        "    path.write_text('artifact', encoding='utf-8')\n",
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any("weak_marker.py:5" in issue and "invalid callsite classification" in issue for issue in issues)


def test_stale_exact_callsite_registry_entry_fails(tmp_path):
    _seed_required_guards(tmp_path)
    registry = tmp_path / "core" / "trust" / "static_sink_registry.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.trusted_sink_registry.v1",
                "entries": {
                    "core/removed.py::write::write_text::deadbeef::1": {
                        "category": "artifact",
                        "owner": "test",
                        "target_class": "artifact",
                        "expires": "never",
                        "reason": "removed fixture",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert "trusted sink registry has 1 stale callsite(s)" in issues


def test_registry_cannot_claim_guard_dominance(tmp_path):
    _seed_required_guards(tmp_path)
    writer = tmp_path / "core" / "forged_guard.py"
    writer.write_text(
        "from pathlib import Path\n\n"
        "def write(path: Path):\n"
        "    path.write_text('formal', encoding='utf-8')\n",
        encoding="utf-8",
    )
    sink_id = scan_direct_writes(tmp_path)["sites"][0].sink_id
    registry = tmp_path / "core" / "trust" / "static_sink_registry.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.trusted_sink_registry.v1",
                "entries": {
                    sink_id: {
                        "category": "guarded_trusted_push",
                        "owner": "test",
                        "target_class": "formal_markdown",
                        "expires": "never",
                        "reason": "forged guard",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    issues = scan_trusted_push_guards(tmp_path)

    assert any("invalid registry category: guarded_trusted_push" in issue for issue in issues)


def _seed_required_guards(root: Path) -> None:
    for rel_path, markers in REQUIRED_GUARDS.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(markers), encoding="utf-8")
