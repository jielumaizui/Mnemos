import hashlib
import json
import subprocess
from pathlib import Path

from scripts import audit_document_asset_manifest as audit
from scripts import run_local_gates


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _active_contract_text(
    *,
    asset_id: str,
    supersedes: list[str],
    renamed_asset_id: str,
    renamed_path: str,
    renamed_sha256: str,
    body: str,
) -> str:
    supersedes_yaml = "".join(f"  - {item}\n" for item in supersedes)
    return (
        "---\n"
        "status: ACTIVE\n"
        "governance_role: current_active\n"
        "authority: SOLE_GOVERNING_CONTRACT\n"
        f"asset_id: {asset_id}\n"
        "supersedes:\n"
        f"{supersedes_yaml}"
        "root_definition_owner: THIS_DOCUMENT\n"
        "root_history_owner: THIS_DOCUMENT\n"
        "current_index_policy: GENERATED_ONLY\n"
        "final_byte_hash_owner: DETACHED_CLOSURE_BUNDLE_ONLY\n"
        "renamed_from:\n"
        f"  asset_id: {renamed_asset_id}\n"
        f"  path: {renamed_path}\n"
        f"  sha256: {renamed_sha256}\n"
        "---\n"
        f"{body}"
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    desktop = tmp_path / "map"
    repo.mkdir()
    desktop.mkdir()
    _git(repo, "init", "-q")
    _git(desktop, "init", "-q")
    _write(repo / "README.md", "# README\n")
    _write(repo / "prompts/distill/extract/base.md", "JSON {output_schema}\n")
    _write(
        repo / "prompts/distill/_output_schemas/extract.json",
        '{"type":"object","properties":{}}\n',
    )
    _write(
        repo / "core/consumer.py",
        "class PromptConsumer:\n"
        "    def load(self):\n"
        "        return None\n\n"
        "class TemplateRegistry:\n"
        "    def _load_all(self, root):\n"
        '        return root.rglob("*.md")\n'
        "    def select(self):\n"
        "        return None\n"
        "    def render_schema(self):\n"
        "        return None\n",
    )
    _write(
        desktop / "00-current.md",
        "# Current\n\nCurrent claim evidence: `99-facts.json#current_state`, "
        "`core/consumer.py`.\n",
    )
    _write(
        desktop / "86-generated.md",
        "# Generated\n\n- Current source commit: `abc123`\n",
    )
    _write(
        desktop / "99-facts.json",
        json.dumps(
            {
                "current_state": {
                    "schema_version": "mnemos.system_map_current_state.v1",
                    "repo_git_commit": "abc123",
                }
            }
        ),
    )
    current_contract = desktop.parent / "current-contract.md"
    historical_contract = desktop.parent / "historical-contract.md"
    _write(
        current_contract,
        _active_contract_text(
            asset_id="desktop:current-contract",
            supersedes=["desktop:historical-contract", "desktop:previous-contract"],
            renamed_asset_id="desktop:previous-contract",
            renamed_path="previous-contract.md",
            renamed_sha256="sha256:" + ("1" * 64),
            body=(
                "Contract status: ACTIVE\n"
                "current anchor\n"
                "phase1-fixture-current\n"
            ),
        ),
    )
    _write(
        repo / "docs/acceptance/cognitive_root_closures.jsonl",
        json.dumps(
            {
                "root_id": "COG-045",
                "machine_artifact": (
                    "docs/acceptance/cognitive_remediation_phase_1_ledger.json"
                    "#phase1-fixture-current"
                ),
            }
        )
        + "\n",
    )
    _write(
        historical_contract,
        "---\n"
        "status: SUPERSEDED_HISTORICAL_EVIDENCE\n"
        "governance_role: historical_provenance\n"
        "asset_id: desktop:historical-contract\n"
        "gate_eligible: false\n"
        "superseded_by: desktop:current-contract\n"
        "authority_for_current_state: NONE\n"
        "mutation_policy: FROZEN\n"
        "---\n"
        "Contract status: SUPERSEDED\n"
        "historical anchor\n",
    )
    _git(repo, "add", ".")
    _git(desktop, "add", ".")
    manifest = repo / "docs/acceptance/document_asset_manifest.json"
    manifest_payload = {
        "schema_version": audit.SCHEMA_VERSION,
        "prompt_version": {"path": "core/consumer.py", "symbol": "PromptConsumer"},
        "exclusions": [],
        "prompt_contracts": [
            {
                "path": "prompts/distill/extract/base.md",
                "sha256": _sha(repo / "prompts/distill/extract/base.md"),
                "consumers": [{"path": "core/consumer.py", "symbol": "TemplateRegistry.select"}],
                "output_schema": "prompts/distill/_output_schemas/extract.json",
                "output_contract": "json_schema",
            },
            {
                "path": "prompts/distill/_output_schemas/extract.json",
                "sha256": _sha(repo / "prompts/distill/_output_schemas/extract.json"),
                "consumers": [
                    {"path": "core/consumer.py", "symbol": "TemplateRegistry.render_schema"}
                ],
                "output_schema": None,
                "output_contract": "schema_definition",
            },
        ],
        "desktop_assets": [
            {
                "path": "00-current.md",
                "classification": "current_contract",
                "evidence": ["99-facts.json#current_state", "core/consumer.py"],
            },
            {
                "path": "86-generated.md",
                "classification": "generated_index",
                "evidence": "repo_git_commit",
            },
            {
                "path": "99-facts.json",
                "classification": "current_state_evidence",
                "evidence": "current_state",
            },
        ],
        "external_governing_assets": [
            {
                "asset_id": "desktop:current-contract",
                "path": current_contract.name,
                "profile": "phase0_required",
                "classification": "governing_contract",
                "governance_role": "current_active",
                "supersedes": [
                    "desktop:historical-contract",
                    "desktop:previous-contract",
                ],
                "renamed_from": {
                    "asset_id": "desktop:previous-contract",
                    "path": "previous-contract.md",
                    "sha256": "sha256:" + ("1" * 64),
                },
                "required_anchors": ["Contract status: ACTIVE", "current anchor"],
                "required_current_root_generations": ["COG-045"],
                "final_byte_hash_owner": "detached_closure_bundle_only",
            }
        ],
        "external_historical_assets": [
            {
                "asset_id": "desktop:historical-contract",
                "path": historical_contract.name,
                "profile": "historical_reference",
                "classification": "historical_source",
                "governance_role": "historical_provenance",
                "gate_eligible": False,
                "superseded_by": "desktop:current-contract",
                "required_anchors": [
                    "Contract status: SUPERSEDED",
                    "historical anchor",
                ],
                "frozen_sha256": _sha(historical_contract),
            }
        ],
    }
    _write(manifest, json.dumps(manifest_payload))
    _git(repo, "add", ".")
    return repo, desktop, manifest


def test_manifest_closes_all_tracked_assets(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert report["ok"] is True
    assert report["counts"] == {
        "repo_markdown_discovered": 2,
        "repo_markdown_reviewed": 2,
        "repo_markdown_excluded": 0,
        "prompt_assets_discovered": 2,
        "prompt_assets_reviewed": 2,
        "desktop_assets_discovered": 3,
        "desktop_assets_reviewed": 3,
        "external_governing_assets_reviewed": 1,
        "external_historical_assets_reviewed": 1,
        "unverified": 0,
    }


def test_new_root_markdown_is_discovered_without_editing_a_path_list(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    _write(repo / "NEW_FORMAL.md", "retired Memos claim\n")
    _git(repo, "add", "NEW_FORMAL.md")

    paths = audit.discover_reviewed_markdown(repo, manifest)

    assert repo / "NEW_FORMAL.md" in paths


def test_desktop_discovery_survives_hermetic_home_via_repo_ancestor(tmp_path, monkeypatch):
    home = tmp_path / "real-home"
    repo = home / "code" / "mnemos"
    desktop = home / "Desktop" / "mnemos系统图谱"
    repo.mkdir(parents=True)
    desktop.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "sandbox-home"))

    assert audit.discover_desktop_root(repo) == desktop


def test_unregistered_prompt_hash_consumer_and_schema_fail_closed(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["prompt_contracts"][0]["sha256"] = "sha256:" + "0" * 64
    payload["prompt_contracts"][0]["consumers"][0]["symbol"] = "Missing.symbol"
    payload["prompt_contracts"][0]["output_schema"] = "prompts/missing.json"
    _write(repo / "prompts/new_prompt.md", "new\n")
    _git(repo, "add", "prompts/new_prompt.md")
    _write(manifest, json.dumps(payload))

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert set(report["by_rule"]) >= {
        "unregistered_prompt_asset",
        "stale_prompt_hash",
        "missing_prompt_consumer_symbol",
        "missing_prompt_schema",
    }


def test_exclusion_requires_owner_reason_expiry_and_existing_path(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["exclusions"] = [{"path": "README.md", "reason": ""}]
    _write(manifest, json.dumps(payload))

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "invalid_exclusion" in report["by_rule"]


def test_desktop_unclassified_stale_generated_and_missing_current_evidence_fail(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    _write(desktop / "07-unclassified.md", "# New\n")
    _git(desktop, "add", "07-unclassified.md")
    _write(desktop / "86-generated.md", "# Generated\n- Current source commit: `old`\n")
    _write(desktop / "00-current.md", "# Current without evidence\n")

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert set(report["by_rule"]) >= {
        "unclassified_desktop_asset",
        "stale_desktop_generated_commit",
        "missing_desktop_current_evidence",
    }


def test_invalid_utf8_desktop_contract_is_never_replacement_decoded(
    tmp_path,
) -> None:
    repo, desktop, manifest = _fixture(tmp_path)
    (desktop / "00-current.md").write_bytes(
        b"# Current\nCurrent claim evidence: "
        b"`99-facts.json#current_state`, `core/consumer.py`.\n\xff"
    )

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert report["ok"] is False
    assert report["by_rule"]["unreadable_desktop_asset"] == 1


def test_skipped_desktop_still_validates_static_classifications(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["desktop_assets"][0]["classification"] = "history_dump"
    _write(manifest, json.dumps(payload))

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
        include_desktop=False,
    )

    assert report["desktop_skipped"] is True
    assert "invalid_desktop_classification" in report["by_rule"]


def test_external_governing_assets_require_exact_shape_and_anchors(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    external = desktop.parent / "handoff.md"
    _write(
        external,
        _active_contract_text(
            asset_id="desktop:handoff",
            supersedes=["desktop:previous-handoff"],
            renamed_asset_id="desktop:previous-handoff",
            renamed_path="previous-handoff.md",
            renamed_sha256="sha256:" + ("2" * 64),
            body="required anchor\nphase1-fixture-current\n",
        ),
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["external_governing_assets"] = [
        {
            "asset_id": "desktop:handoff",
            "path": "handoff.md",
            "profile": "phase0_required",
            "classification": "governing_contract",
            "governance_role": "current_active",
            "supersedes": ["desktop:previous-handoff"],
            "renamed_from": {
                "asset_id": "desktop:previous-handoff",
                "path": "previous-handoff.md",
                "sha256": "sha256:" + ("2" * 64),
            },
            "required_anchors": ["required anchor"],
            "required_current_root_generations": ["COG-045"],
            "final_byte_hash_owner": "detached_closure_bundle_only",
        }
    ]
    payload["external_historical_assets"] = []
    _write(manifest, json.dumps(payload))

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )
    assert report["ok"] is True
    assert report["counts"]["external_governing_assets_reviewed"] == 1

    payload["external_governing_assets"][0]["profile"] = "optional"
    payload["external_governing_assets"][0]["required_anchors"] = []
    _write(manifest, json.dumps(payload))
    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )
    assert "invalid_external_governing_asset" in report["by_rule"]


def test_external_governing_asset_requires_current_machine_generation_anchor(
    tmp_path,
):
    repo, desktop, manifest = _fixture(tmp_path)
    current = desktop.parent / "current-contract.md"
    _write(
        current,
        current.read_text(encoding="utf-8").replace(
            "phase1-fixture-current\n",
            "",
        ),
    )

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "stale_external_governing_generation" in report["by_rule"]


def test_external_governing_assets_reject_multiple_active_contract_owners(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["external_governing_assets"] = []
    for index in (1, 2):
        path = f"contract-{index}.md"
        _write(
            desktop.parent / path,
            _active_contract_text(
                asset_id=f"desktop:contract-{index}",
                supersedes=[f"desktop:previous-contract-{index}"],
                renamed_asset_id=f"desktop:previous-contract-{index}",
                renamed_path=f"previous-contract-{index}.md",
                renamed_sha256="sha256:" + (str(index) * 64),
                body="Contract status: ACTIVE\nrequired anchor\n",
            ),
        )
        payload["external_governing_assets"].append(
            {
                "asset_id": f"desktop:contract-{index}",
                "path": path,
                "profile": "phase0_required",
                "classification": "governing_contract",
                "governance_role": "current_active",
                "supersedes": [f"desktop:previous-contract-{index}"],
                "renamed_from": {
                    "asset_id": f"desktop:previous-contract-{index}",
                    "path": f"previous-contract-{index}.md",
                    "sha256": "sha256:" + (str(index) * 64),
                },
                "required_anchors": ["Contract status: ACTIVE", "required anchor"],
                "final_byte_hash_owner": "detached_closure_bundle_only",
            }
        )
    _write(manifest, json.dumps(payload))

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "external_governing_owner_count" in report["by_rule"]


def test_external_historical_asset_must_bind_to_the_single_active_contract(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    current_path = desktop.parent / "current-contract.md"
    historical_path = desktop.parent / "historical-contract.md"
    _write(
        current_path,
        _active_contract_text(
            asset_id="desktop:current-contract",
            supersedes=["desktop:historical-contract", "desktop:previous-contract"],
            renamed_asset_id="desktop:previous-contract",
            renamed_path="previous-contract.md",
            renamed_sha256="sha256:" + ("1" * 64),
            body=(
                "Contract status: ACTIVE\n"
                "current anchor\n"
                "phase1-fixture-current\n"
            ),
        ),
    )
    _write(
        historical_path,
        "---\n"
        "status: SUPERSEDED_HISTORICAL_EVIDENCE\n"
        "governance_role: historical_provenance\n"
        "asset_id: desktop:historical-contract\n"
        "gate_eligible: false\n"
        "superseded_by: desktop:current-contract\n"
        "authority_for_current_state: NONE\n"
        "mutation_policy: FROZEN\n"
        "---\n"
        "Contract status: SUPERSEDED\n"
        "historical anchor\n",
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["external_governing_assets"] = [
        {
            "asset_id": "desktop:current-contract",
            "path": current_path.name,
            "profile": "phase0_required",
            "classification": "governing_contract",
            "governance_role": "current_active",
            "supersedes": [
                "desktop:historical-contract",
                "desktop:previous-contract",
            ],
            "renamed_from": {
                "asset_id": "desktop:previous-contract",
                "path": "previous-contract.md",
                "sha256": "sha256:" + ("1" * 64),
            },
            "required_anchors": ["Contract status: ACTIVE", "current anchor"],
            "required_current_root_generations": ["COG-045"],
            "final_byte_hash_owner": "detached_closure_bundle_only",
        }
    ]
    payload["external_historical_assets"] = [
        {
            "asset_id": "desktop:historical-contract",
            "path": historical_path.name,
            "profile": "historical_reference",
            "classification": "historical_source",
            "governance_role": "historical_provenance",
            "gate_eligible": False,
            "superseded_by": "desktop:current-contract",
            "required_anchors": ["Contract status: SUPERSEDED", "historical anchor"],
            "frozen_sha256": _sha(historical_path),
        }
    ]
    _write(manifest, json.dumps(payload))

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )
    assert report["ok"] is True

    payload["external_historical_assets"][0]["superseded_by"] = "desktop:other-contract"
    _write(manifest, json.dumps(payload))
    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "invalid_external_historical_supersession" in report["by_rule"]


def test_external_historical_asset_hash_drift_is_blocking(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    historical = desktop.parent / "historical-contract.md"
    _write(historical, historical.read_text(encoding="utf-8") + "mutated\n")

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "stale_external_historical_hash" in report["by_rule"]


def test_external_governing_authority_must_be_in_frontmatter(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    current = desktop.parent / "current-contract.md"
    text = current.read_text(encoding="utf-8").replace(
        "status: ACTIVE",
        "status: DRAFT",
        1,
    )
    _write(current, text + "\nstatus: ACTIVE\n")

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "invalid_external_governing_authority_header" in report["by_rule"]


def test_external_governing_frontmatter_rejects_duplicate_conflicting_scalar(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    current = desktop.parent / "current-contract.md"
    text = current.read_text(encoding="utf-8").replace(
        "status: ACTIVE\n",
        "status: ACTIVE\nstatus: DRAFT\n",
        1,
    )
    _write(current, text)

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "invalid_external_governing_authority_header" in report["by_rule"]


def test_external_governing_frontmatter_rejects_self_owned_final_hash(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    current = desktop.parent / "current-contract.md"
    text = current.read_text(encoding="utf-8").replace(
        "final_byte_hash_owner: DETACHED_CLOSURE_BUNDLE_ONLY",
        "final_byte_hash_owner: THIS_DOCUMENT",
        1,
    )
    _write(current, text)

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "invalid_external_governing_authority_header" in report["by_rule"]


def test_external_governing_frontmatter_rejects_wrongly_nested_supersedes(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    current = desktop.parent / "current-contract.md"
    text = current.read_text(encoding="utf-8").replace(
        "supersedes:\n" "  - desktop:historical-contract\n" "  - desktop:previous-contract\n",
        "metadata:\n"
        "  supersedes:\n"
        "    - desktop:historical-contract\n"
        "    - desktop:previous-contract\n",
        1,
    )
    _write(current, text)

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "invalid_external_governing_authority_header" in report["by_rule"]


def test_external_governing_predecessor_must_remain_retired(tmp_path):
    repo, desktop, manifest = _fixture(tmp_path)
    _write(desktop.parent / "previous-contract.md", "unexpected retired contract\n")

    report = audit.audit_assets(
        repo_root=repo,
        manifest_path=manifest,
        desktop_root=desktop,
        current_commit="abc123",
    )

    assert "active_external_governing_predecessor" in report["by_rule"]


def test_local_gates_include_document_asset_manifest_audit():
    gate_commands = {name: command for name, command in run_local_gates.GATES}

    assert gate_commands["document asset manifest audit"] == [
        "python",
        "scripts/audit_document_asset_manifest.py",
        "--strict",
    ]


def test_precommit_ci_and_full_score_use_the_manifest_gate():
    root = Path(__file__).resolve().parents[2]
    precommit = (root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    full_score = (root / "scripts/run_full_score_gates.py").read_text(encoding="utf-8")

    assert "scripts/audit_document_asset_manifest.py --strict" in precommit
    assert "scripts/audit_document_asset_manifest.py --strict --desktop-mode skip" in ci
    assert '"docs.asset_manifest.strict"' in full_score
    assert '"required"' in full_score
