from __future__ import annotations

from pathlib import Path

from scripts.audit_config_registry_closure import scan_read_sites


def test_scan_read_sites_finds_config_get_and_helper(tmp_path: Path):
    path = tmp_path / "core" / "sample.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "value = config.get('distill.token_budget_total', 1)\n"
        "other = _cfg_get(cfg, 'capture.max_workers', 2)\n",
        encoding="utf-8",
    )

    sites = scan_read_sites(tmp_path)
    assert [(item.key, item.caller_default) for item in sites] == [
        ("distill.token_budget_total", "1"),
        ("capture.max_workers", "2"),
    ]


def test_scan_read_sites_ignores_nested_mapping_gets_but_catches_unknown_dotted_key(
    tmp_path: Path,
):
    path = tmp_path / "core" / "sample.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "provider_cfg.get('api_key', '')\n"
        "config.get('future.unknown_key', 1)\n"
        "runtime_cfg = get_config()\n"
        "runtime_cfg.get('future_single_key')\n"
        "for service, key, default in ((\"worker\", \"future.dynamic_key\", 3),):\n"
        "    config.get(key, default)\n",
        encoding="utf-8",
    )

    sites = scan_read_sites(tmp_path)
    assert [(item.key, item.caller_default) for item in sites] == [
        ("future.unknown_key", "1"),
        ("future_single_key", ""),
        ("future.dynamic_key", "3"),
    ]


def test_real_registry_has_no_unknown_removed_or_divergent_readers():
    from scripts.audit_config_registry_closure import audit

    report = audit(root=Path.cwd(), live_config_path=None)
    assert report["unknown_reader_count"] == 0
    assert report["removed_reader_count"] == 0
    assert report["divergent_fallback_count"] == 0
