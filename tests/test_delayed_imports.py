"""T15: Chronos runtime delayed-import governance ratchet.

Ensures no new function-local imports are added to the Chronos runtime modules
without an explicit waiver. Existing delayed imports are tracked in
``scripts/delayed_import_waivers.json`` with documented justifications.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.audit_delayed_imports import (
    DEFAULT_SCAN_FILES,
    DEFAULT_WAIVER_FILE,
    PROJECT_ROOT,
    audit,
    load_waivers,
)


@pytest.mark.parametrize("scan_file", DEFAULT_SCAN_FILES)
def test_no_unwaived_delayed_imports(scan_file: str) -> None:
    """All delayed imports in the governed files must be in the waiver baseline."""
    scan_path = PROJECT_ROOT / scan_file
    waivers = load_waivers(Path(DEFAULT_WAIVER_FILE))
    _, unwaived = audit([scan_path], PROJECT_ROOT, waivers)
    assert not unwaived, "Found unwaived delayed imports:\n" + "\n".join(
        f"  {f.file}:{f.line}:{f.col} {f.function}() imports {f.module}" for f in unwaived
    )


def test_delayed_import_waiver_requires_reason(tmp_path: Path) -> None:
    waiver_file = tmp_path / "waivers.json"
    waiver_file.write_text(
        '[{"file":"core/kia/chronos.py","function":"*","module":"core.config"}]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-empty reason"):
        load_waivers(waiver_file)
