"""COG-025 purity and privacy contract for the ingest-helper hot path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from core.ops.hermetic_run import HermeticRunEnvironment


ROOT = Path(__file__).resolve().parents[2]
BASELINE_COMMIT = "dd60d1c7e4ee96f4982a7ec51acfe5b3bca6af6c"
BASELINE_SOURCE_SHA256 = "124dba725b66a7e8fb2c5b4afdee17169e5a4a8c91d1cff8a15a1c83ea8c5fdb"
WORKLOAD = "这是一个用于验证纯函数路径不会写入文件系统的有效技术讨论。"

_CHILD_PROGRAM = r'''
import importlib.util
import json
import os
import sys

write_events = []
write_event_names = {
    "os.chmod",
    "os.chown",
    "os.link",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.symlink",
    "os.truncate",
    "os.utime",
}

def is_write_open(args):
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
        return True
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    return isinstance(flags, int) and bool(flags & write_flags)

def audit_hook(event, args):
    if event == "open" and is_write_open(args):
        write_events.append(event)
    elif event in write_event_names:
        write_events.append(event)

sys.addaudithook(audit_hook)
source_path = os.environ.get("MNEMOS_PURITY_BASELINE_SOURCE")
if source_path:
    spec = importlib.util.spec_from_file_location("pre_cog025_ingest_helpers", source_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    is_noise_message = module.is_noise_message
    score_message_quality = module.score_message_quality
else:
    from core.kia.ingest_helpers import is_noise_message, score_message_quality

content = os.environ["MNEMOS_PURITY_WORKLOAD"]
for _ in range(100_000):
    assert is_noise_message(content) is False
for _ in range(100_000):
    assert score_message_quality(content)["total_score"] >= 0

print(json.dumps({
    "filesystem_write_count": len(write_events),
    "write_event_names": sorted(set(write_events)),
}, sort_keys=True))
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int, str]]:
    snapshot: dict[str, tuple[str, int, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = ("symlink", 0, os.readlink(path))
        elif path.is_file():
            snapshot[relative] = ("file", path.stat().st_size, _sha256(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", 0, "")
    return snapshot


def _count_occurrences(root: Path, needle: bytes) -> int:
    count = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as handle:
            previous = b""
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                count += (previous + chunk).count(needle)
                previous = chunk[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return count


def _frozen_baseline_source(tmp_path: Path) -> Path:
    completed = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:core/kia/ingest_helpers.py"],
        cwd=ROOT,
        text=False,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert hashlib.sha256(completed.stdout).hexdigest() == BASELINE_SOURCE_SHA256
    source_path = tmp_path / "pre_cog025_ingest_helpers.py"
    source_path.write_bytes(completed.stdout)
    return source_path


def _run_workload(
    run: HermeticRunEnvironment,
    *,
    baseline_source: Path | None = None,
    os_write_guard: bool,
) -> dict[str, object]:
    environment = {
        **run.environment,
        "MNEMOS_PURITY_WORKLOAD": WORKLOAD,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if baseline_source is not None:
        environment["MNEMOS_PURITY_BASELINE_SOURCE"] = str(baseline_source)

    before = _tree_snapshot(run.root)
    command = [sys.executable, "-B", "-c", _CHILD_PROGRAM]
    if os_write_guard:
        sandbox_exec = shutil.which("sandbox-exec")
        assert sandbox_exec is not None, "macOS COG-025 contract requires sandbox-exec"
        command = [
            sandbox_exec,
            "-p",
            "(version 1) (allow default) (deny file-write*)",
            *command,
        ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    after = _tree_snapshot(run.root)
    report: dict[str, object] = {}
    if completed.returncode == 0:
        report = json.loads(completed.stdout)
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "report": report,
        "created_paths": sorted(set(after) - set(before)),
        "modified_paths": sorted(
            path for path in set(after) & set(before) if after[path] != before[path]
        ),
        "os_write_guard": os_write_guard,
    }


def test_frozen_pre_cog025_implementation_is_detected_as_non_pure(tmp_path: Path) -> None:
    """The immutable baseline must violate the same 100k-call contract."""
    baseline_source = _frozen_baseline_source(tmp_path)
    run = HermeticRunEnvironment.create(tmp_path / "baseline-run", profile="isolated")
    result = _run_workload(run, baseline_source=baseline_source, os_write_guard=False)
    report = result["report"]

    assert result["returncode"] == 0, result["stderr"]
    assert isinstance(report, dict)
    assert report["filesystem_write_count"] > 0
    assert result["created_paths"]
    assert any(path.endswith("rule_scorer_bypass.log") for path in result["created_paths"])
    assert _count_occurrences(run.root, b'"content_preview"') > 0
    assert _count_occurrences(run.root, WORKLOAD.encode("utf-8")) > 0


def test_ingest_helper_hot_paths_are_filesystem_pure_and_do_not_leak_content(
    tmp_path: Path,
) -> None:
    """Default imports and 100k calls stay pure under an OS-level write guard."""
    run = HermeticRunEnvironment.create(tmp_path / "candidate-run", profile="isolated")
    result = _run_workload(
        run,
        os_write_guard=sys.platform == "darwin",
    )
    report = result["report"]

    assert result["returncode"] == 0, result["stderr"]
    assert isinstance(report, dict)
    assert report["filesystem_write_count"] == 0, report
    assert result["created_paths"] == []
    assert result["modified_paths"] == []
    assert _count_occurrences(run.root, b'"content_preview"') == 0
    assert _count_occurrences(run.root, WORKLOAD.encode("utf-8")) == 0
    assert WORKLOAD not in str(result["stdout"])
    assert WORKLOAD not in str(result["stderr"])
