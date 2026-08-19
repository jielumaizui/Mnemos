from __future__ import annotations

import argparse

from core.cli.commands.setup import _auto_setup_namespace


def test_auto_setup_namespace_matches_auto_setup_contract() -> None:
    args = argparse.Namespace(
        yes=True,
        skip_backend=True,
        skip_daemon=True,
        skip_scheduler=True,
        skip_hooks=True,
        skip_verify=True,
        skip_backfill=True,
        skip_e2e=True,
        preserve_config=True,
        max_smoke_attempts=3,
    )

    namespace = _auto_setup_namespace(args)
    values = vars(namespace)
    reexec_entrypoint = values.pop("reexec_entrypoint")

    assert reexec_entrypoint.endswith("mnemos_cli.py")
    assert values == {
        "yes": True,
        "dry_run": False,
        "skip_backend": True,
        "skip_daemon": True,
        "skip_scheduler": True,
        "skip_hooks": True,
        "skip_verify": True,
        "skip_backfill": True,
        "skip_e2e": True,
        "preserve_config": True,
        "max_smoke_attempts": 3,
        "venv_reexec": False,
        "reexec_args": [
            "setup",
            "--yes",
            "--skip-backend",
            "--skip-daemon",
            "--skip-scheduler",
            "--skip-hooks",
            "--skip-verify",
            "--skip-backfill",
            "--skip-e2e",
            "--preserve-config",
            "--max-smoke-attempts",
            "3",
        ],
    }
