"""Init command for Mnemos CLI."""

import argparse
import sys


def cmd_init(args):
    """交互式配置向导。

    `mnemos init` 复用 auto_setup 的部署链路，避免维护第二套过时的
    LLM/Embedding/Reranker 配置向导。
    """
    from scripts.auto_setup import SetupAbort, _run_setup, print_err

    setup_args = argparse.Namespace(
        yes=bool(getattr(args, "yes", False)),
        skip_backend=bool(getattr(args, "skip_backend", True)),
        skip_daemon=bool(getattr(args, "skip_daemon", True)),
        skip_scheduler=bool(getattr(args, "skip_scheduler", True)),
        skip_hooks=bool(getattr(args, "skip_hooks", False)),
        skip_verify=bool(getattr(args, "skip_verify", False)),
        skip_backfill=True,
        skip_e2e=True,
        preserve_config=bool(getattr(args, "preserve_config", False)),
        max_smoke_attempts=int(getattr(args, "max_smoke_attempts", 3)),
        venv_reexec=True,
    )
    try:
        _run_setup(setup_args)
    except SetupAbort as e:
        print_err(str(e))
        sys.exit(1)
