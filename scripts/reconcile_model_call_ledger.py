#!/usr/bin/env python3
"""CLI adapter for registered model-call-ledger reconciliation.

All planning, backup, cleanup, and apply behavior lives under
``core.migrations.model_call_ledger_reconcile``.  This script deliberately
exposes no internal helpers or direct-apply capability.
"""

import sys
from pathlib import Path


# This script is a supported direct entry point, so it must resolve the
# repository package when invoked by absolute path from outside the repo.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.migrations.model_call_ledger_reconcile.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
