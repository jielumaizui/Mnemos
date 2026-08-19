"""Private implementation module for successor_d0_verification.snapshot."""

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path

from typing import Any

from typing import Callable

from typing import Mapping

from typing import Sequence

import ast

import hashlib

import os

import re

import selectors

import stat

import subprocess

import sys

import tarfile

import time

from .wire import (
    ARTIFACT_SCHEMAS,
    Finding,
    GIT_ARCHIVE_TIMEOUT_SECONDS,
    GIT_COMMAND_TIMEOUT_SECONDS,
    MANIFEST_SCHEMA,
    MAX_SNAPSHOT_ARCHIVE_BYTES,
    MAX_SNAPSHOT_BLOB_BYTES,
    MAX_SNAPSHOT_FILE_COUNT,
    MAX_SNAPSHOT_TOTAL_BYTES,
    REQUIRED_BINDING_KINDS,
    SUCCESSOR_CONSTITUTION_ANCHORS,
    _GIT_OBJECT_ID,
    _SHA256_REF,
    _VerifierResourceLimit,
    _canonical_json_bytes,
    _finding,
    _read_exact_regular_file,
    _sha256,
)

_EXPECTED_GENERATOR_IMPLEMENTATION_PATHS = tuple(
    sorted(
        {
            "scripts/generate_successor_d0_catalog.py",
            "scripts/successor_d0_catalog.py",
            "scripts/successor_d0_generation/__init__.py",
            "scripts/successor_d0_generation/builder.py",
            "scripts/successor_d0_generation/cli_inventory.py",
            "scripts/successor_d0_generation/contract_inventory.py",
            "scripts/successor_d0_generation/model.py",
            "scripts/successor_d0_generation/repository_inventory.py",
            "scripts/successor_d0_generation/runtime_inventory.py",
            "scripts/successor_d0_generation/snapshot.py",
            "scripts/successor_d0_generation/static_python.py",
        }
    )
)


@dataclass(frozen=True)
class _VerifierTreeBlob:
    """One regular Git blob independently expected by the verifier."""

    path: str
    mode: str
    object_id: str
    size: int


_MAX_GIT_STDERR_BYTES = 64 * 1024

_MAX_TREE_LISTING_BYTES = 64 * 1024 * 1024

_MAX_TREE_RECORD_BYTES = 16 * 1024


def _python_module_for_path(path: str) -> str:
    suffix = "/__init__.py"
    if path.endswith(suffix):
        return path[: -len(suffix)].replace("/", ".")
    if path.endswith(".py"):
        return path[:-3].replace("/", ".")
    raise ValueError(f"generator identity contains a non-Python path: {path}")


def _generator_import_closure_errors(sources: Mapping[str, bytes]) -> list[str]:
    """Reject unbound Python imports and dynamic import escape hatches."""

    declared_modules = {_python_module_for_path(path) for path in sources}
    declared_prefixes = set(declared_modules)
    # Parent packages are structural names, not extra executable modules.
    for module in tuple(declared_modules):
        parts = module.split(".")
        declared_prefixes.update(".".join(parts[:index]) for index in range(1, len(parts)))

    errors: list[str] = []
    for path, raw in sorted(sources.items()):
        try:
            tree = ast.parse(raw.decode("utf-8"), filename=path)
        except (UnicodeError, SyntaxError) as exc:
            errors.append(f"{path}: cannot parse bound generator source: {exc}")
            continue
        module = _python_module_for_path(path)
        package_parts = (
            module.split(".") if path.endswith("/__init__.py") else module.split(".")[:-1]
        )
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    retained = len(package_parts) - (node.level - 1)
                    if retained <= 0:
                        errors.append(f"{path}:{node.lineno}: relative import escapes package")
                        continue
                    base_parts = package_parts[:retained]
                    if node.module:
                        base_parts.extend(node.module.split("."))
                    imported.append(".".join(base_parts))
                elif node.module:
                    imported.append(node.module)
            elif isinstance(node, ast.Call):
                dynamic_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                )
                if dynamic_name in {"__import__", "import_module"}:
                    errors.append(
                        f"{path}:{node.lineno}: dynamic import is outside exact-file-set-v1"
                    )
            for imported_module in imported:
                top_level = imported_module.split(".", 1)[0]
                if imported_module in declared_prefixes or top_level in sys.stdlib_module_names:
                    continue
                errors.append(
                    f"{path}:{getattr(node, 'lineno', 0)}: " f"unbound import {imported_module!r}"
                )
    return errors


def _verifier_stream_git(
    repo_root: Path,
    arguments: Sequence[str],
    *,
    label: str,
    timeout_seconds: int,
    max_stdout_bytes: int,
    consume_stdout: Callable[[bytes], object],
) -> int:
    """Run Git with verifier-owned streaming byte and wall-clock bounds."""

    process = subprocess.Popen(
        ["git", "-C", str(repo_root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
        process.kill()
        process.wait()
        raise ValueError(f"{label} did not expose bounded pipes")
    selector = selectors.DefaultSelector()
    stderr = bytearray()
    stdout_bytes = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        for stream, stream_name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, stream_name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"{label} timed out after {timeout_seconds} seconds")
            events = selector.select(min(remaining, 1.0))
            if not events:
                continue
            for key, _mask in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    stdout_bytes += len(chunk)
                    if stdout_bytes > max_stdout_bytes:
                        raise ValueError(f"{label} exceeds byte limit {max_stdout_bytes}")
                    consume_stdout(chunk)
                elif len(stderr) < _MAX_GIT_STDERR_BYTES:
                    stderr.extend(chunk[: _MAX_GIT_STDERR_BYTES - len(stderr)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f"{label} timed out after {timeout_seconds} seconds")
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise ValueError(f"{label} timed out after {timeout_seconds} seconds") from exc
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if returncode:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or f"{label} failed")
    return stdout_bytes


def _git_bytes(repo_root: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{revision}:{path}"],
        check=True,
        capture_output=True,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    return result.stdout


def _git_value(repo_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def _verifier_safe_tree_path(raw_path: bytes | str, *, source: str) -> str:
    try:
        value = (
            raw_path.decode("utf-8", errors="strict") if isinstance(raw_path, bytes) else raw_path
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"non-UTF-8 path in {source}") from exc
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        )
    ):
        raise ValueError(f"unsafe or abnormal path in {source}: {value!r}")
    return "/".join(parts)


def _verifier_tree_inventory(
    repo_root: Path,
    commit: str,
    object_format: str,
) -> tuple[_VerifierTreeBlob, ...]:
    oid_length = {"sha1": 40, "sha256": 64}.get(object_format)
    if oid_length is None:
        raise ValueError(f"unsupported Git object format: {object_format!r}")
    result: list[_VerifierTreeBlob] = []
    seen: set[str] = set()
    total_size = 0
    pending = bytearray()

    def accept_entry(raw_entry: bytes) -> None:
        nonlocal total_size
        try:
            header, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_kind, raw_oid, raw_size = header.split()
            mode = raw_mode.decode("ascii")
            kind = raw_kind.decode("ascii")
            object_id = raw_oid.decode("ascii")
            size = int(raw_size.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError("malformed git ls-tree entry") from exc
        path = _verifier_safe_tree_path(raw_path, source="git ls-tree")
        if path in seen:
            raise ValueError(f"duplicate path in git tree: {path!r}")
        seen.add(path)
        if kind != "blob" or mode not in {"100644", "100755"}:
            if mode == "120000":
                label = "symlink"
            elif kind == "commit" or mode == "160000":
                label = "gitlink"
            else:
                label = f"unsupported {kind}/{mode}"
            raise ValueError(f"{label} is not allowed in D0 snapshot: {path}")
        if len(object_id) != oid_length or not re.fullmatch(r"[0-9a-f]+", object_id):
            raise ValueError(f"invalid Git blob object ID for {path}")
        if size < 0 or size > MAX_SNAPSHOT_BLOB_BYTES:
            raise ValueError(
                f"snapshot blob exceeds limit {MAX_SNAPSHOT_BLOB_BYTES}: {path} ({size})"
            )
        result.append(_VerifierTreeBlob(path=path, mode=mode, object_id=object_id, size=size))
        if len(result) > MAX_SNAPSHOT_FILE_COUNT:
            raise ValueError(f"snapshot file count exceeds limit {MAX_SNAPSHOT_FILE_COUNT}")
        total_size += size
        if total_size > MAX_SNAPSHOT_TOTAL_BYTES:
            raise ValueError(f"snapshot total bytes exceed limit {MAX_SNAPSHOT_TOTAL_BYTES}")

    def consume_listing(chunk: bytes) -> None:
        pending.extend(chunk)
        while True:
            separator = pending.find(b"\0")
            if separator < 0:
                break
            raw_entry = bytes(pending[:separator])
            del pending[: separator + 1]
            if raw_entry:
                accept_entry(raw_entry)
        if len(pending) > _MAX_TREE_RECORD_BYTES:
            raise ValueError(f"git ls-tree record exceeds byte limit {_MAX_TREE_RECORD_BYTES}")

    _verifier_stream_git(
        repo_root,
        ["ls-tree", "-r", "-z", "-l", "--full-tree", commit],
        label="git ls-tree",
        timeout_seconds=GIT_COMMAND_TIMEOUT_SECONDS,
        max_stdout_bytes=_MAX_TREE_LISTING_BYTES,
        consume_stdout=consume_listing,
    )
    if pending:
        raise ValueError("git ls-tree output is not NUL terminated")
    return tuple(result)


def _verifier_blob_oid(path: Path, expected_size: int, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {expected_size}\0".encode("ascii"))
    observed_size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > expected_size or observed_size > MAX_SNAPSHOT_BLOB_BYTES:
                raise ValueError(f"snapshot file changed or exceeded limit: {path}")
            digest.update(chunk)
    if observed_size != expected_size:
        raise ValueError(f"snapshot file size changed while hashing: {path}")
    return digest.hexdigest()


def _materialize_snapshot(repo_root: Path, commit: str, destination: Path) -> Path:
    """Extract and byte-verify one immutable Git tree without worktree writes."""

    object_format = _git_value(repo_root, "rev-parse", "--show-object-format")
    expected_entries = _verifier_tree_inventory(repo_root, commit, object_format)
    expected = {entry.path: entry for entry in expected_entries}
    expected_directories = {
        "/".join(entry.path.split("/")[:index])
        for entry in expected_entries
        for index in range(1, len(entry.path.split("/")))
    }
    archive_path = destination / "snapshot.tar"
    snapshot_root = destination / "tree"
    snapshot_root.mkdir()
    with archive_path.open("xb") as archive_file:
        archive_size = _verifier_stream_git(
            repo_root,
            ["archive", "--format=tar", commit],
            label="git archive",
            timeout_seconds=GIT_ARCHIVE_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_SNAPSHOT_ARCHIVE_BYTES,
            consume_stdout=archive_file.write,
        )
    if archive_path.stat().st_size != archive_size:
        raise ValueError("git archive stream byte count differs from durable file size")

    actual_paths: set[str] = set()
    actual_total = 0
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            relative = _verifier_safe_tree_path(member.name, source="git archive")
            target = snapshot_root.joinpath(*relative.split("/"))
            if member.isdir():
                if relative not in expected_directories:
                    raise ValueError(f"unexpected directory in git archive: {relative}")
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                raise ValueError(f"symlink is not allowed in git archive: {relative}")
            if member.islnk():
                raise ValueError(f"hard link is not allowed in git archive: {relative}")
            if not member.isfile():
                raise ValueError(f"unsupported git archive member: {relative}")
            expected_entry = expected.get(relative)
            if expected_entry is None:
                raise ValueError(
                    f"git archive contains path absent from ls-tree snapshot: {relative}"
                )
            if relative in actual_paths:
                raise ValueError(f"duplicate regular file in git archive: {relative}")
            if member.size != expected_entry.size or member.size > MAX_SNAPSHOT_BLOB_BYTES:
                raise ValueError(f"git archive blob size differs from ls-tree snapshot: {relative}")
            actual_paths.add(relative)
            actual_total += member.size
            if (
                len(actual_paths) > MAX_SNAPSHOT_FILE_COUNT
                or actual_total > MAX_SNAPSHOT_TOTAL_BYTES
            ):
                raise ValueError("git archive exceeds snapshot resource limits")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"unreadable git archive member: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            observed_size = 0
            with target.open("xb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    observed_size += len(chunk)
                    if observed_size > expected_entry.size:
                        raise ValueError(f"git archive member exceeded expected size: {relative}")
                    output.write(chunk)
            if observed_size != expected_entry.size:
                raise ValueError(f"git archive member was truncated: {relative}")
            os.chmod(target, 0o755 if expected_entry.mode == "100755" else 0o644)

    missing = sorted(set(expected) - actual_paths)
    if missing:
        raise ValueError(
            "git archive differs from ls-tree snapshot; missing paths: " + ", ".join(missing[:3])
        )
    for relative, expected_entry in expected.items():
        target = snapshot_root.joinpath(*relative.split("/"))
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"snapshot target is not a regular file: {relative}")
        if metadata.st_size != expected_entry.size:
            raise ValueError(f"snapshot size mismatch: {relative}")
        if bool(metadata.st_mode & 0o111) != (expected_entry.mode == "100755"):
            raise ValueError(f"snapshot mode mismatch: {relative}")
        actual_oid = _verifier_blob_oid(target, expected_entry.size, object_format)
        if actual_oid != expected_entry.object_id:
            raise ValueError(f"git archive blob OID differs from ls-tree snapshot: {relative}")
    archive_path.unlink()
    return snapshot_root


def _verify_snapshot(
    repo_root: Path,
    snapshot: Mapping[str, Any],
    findings: list[Finding],
) -> tuple[str, str] | None:
    commit = snapshot.get("commit")
    tree = snapshot.get("tree")
    if (
        not isinstance(commit, str)
        or not isinstance(tree, str)
        or not _GIT_OBJECT_ID.fullmatch(commit)
        or not _GIT_OBJECT_ID.fullmatch(tree)
    ):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "legacy_snapshot must contain full lowercase Git commit and tree object IDs",
                "regenerate against one exact Git commit and tree object",
            )
        )
        return None
    try:
        actual_commit = _git_value(repo_root, "rev-parse", f"{commit}^{{commit}}")
        actual_tree = _git_value(repo_root, "rev-parse", f"{commit}^{{tree}}")
        actual_object_format = _git_value(repo_root, "rev-parse", "--show-object-format")
        entries = _verifier_tree_inventory(repo_root, actual_commit, actual_object_format)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
        findings.append(
            _finding(
                "SNAPSHOT_MISMATCH",
                "manifest",
                f"legacy snapshot cannot be resolved: {exc}",
                "restore the exact Git object before verification",
            )
        )
        return None
    expected_snapshot = {
        "archive_format": "git-archive-tar+ls-tree-blob-oid-v1",
        "commit": actual_commit,
        "file_count": len(entries),
        "git_object_format": actual_object_format,
        "requested_commit": actual_commit,
        "total_blob_bytes": sum(entry.size for entry in entries),
        "tree": actual_tree,
    }
    if dict(snapshot) != expected_snapshot:
        findings.append(
            _finding(
                "SNAPSHOT_MISMATCH",
                "manifest",
                "legacy_snapshot metadata differs from the independently enumerated exact tree",
                "regenerate commit/tree/object-format/archive/count/byte metadata together",
            )
        )
    return actual_commit, actual_tree


def _binding_bytes(
    binding: Mapping[str, Any],
    *,
    repo_root: Path,
    legacy_commit: str,
    external_bindings: Mapping[str, Path],
) -> bytes | None:
    binding_id = str(binding.get("binding_id") or "")
    kind = str(binding.get("binding_kind") or "")
    repo_path = binding.get("repo_path")
    if kind == "legacy_repo_file":
        if not isinstance(repo_path, str) or not repo_path:
            return None
        return _git_bytes(repo_root, legacy_commit, repo_path)
    override = external_bindings.get(binding_id)
    if kind != "external_exact_file" or override is None:
        return None
    return _read_exact_regular_file(override)


def _verify_bindings(
    bindings: object,
    *,
    repo_root: Path,
    legacy_commit: str,
    external_bindings: Mapping[str, Path],
    findings: list[Finding],
) -> None:
    if not isinstance(bindings, list):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                "source_bindings must be an ordered list",
                "regenerate source bindings using the D0 manifest schema",
            )
        )
        return
    binding_ids = [str(item.get("binding_id") or "") for item in bindings if isinstance(item, dict)]
    if binding_ids != sorted(binding_ids) or len(binding_ids) != len(set(binding_ids)):
        findings.append(
            _finding(
                "SOURCE_BINDING_INVALID",
                "manifest",
                "source bindings are unsorted, duplicated, or malformed",
                "sort unique source bindings by binding_id and regenerate",
            )
        )
    if set(binding_ids) != set(REQUIRED_BINDING_KINDS):
        findings.append(
            _finding(
                "SOURCE_BINDING_INVALID",
                "manifest",
                "source binding IDs differ from the complete D0 binding contract",
                "restore every exact required binding and remove unknown bindings",
            )
        )
    for binding in bindings:
        if not isinstance(binding, dict):
            findings.append(
                _finding(
                    "SOURCE_BINDING_INVALID",
                    "manifest",
                    "source binding is not an object",
                    "emit one typed object for each binding",
                )
            )
            continue
        binding_id = str(binding.get("binding_id") or "")
        expected = REQUIRED_BINDING_KINDS.get(binding_id)
        if expected is None:
            findings.append(
                _finding(
                    "SOURCE_BINDING_INVALID",
                    "manifest",
                    f"unknown source binding: {binding_id!r}",
                    "remove the unknown binding and regenerate",
                    record_id=binding_id or None,
                )
            )
            continue
        expected_kind, expected_repo_path = expected
        if binding.get("binding_kind") != expected_kind or (
            expected_repo_path is not None and binding.get("repo_path") != expected_repo_path
        ):
            findings.append(
                _finding(
                    "SOURCE_BINDING_INVALID",
                    "manifest",
                    f"binding kind/path differs from contract for {binding_id!r}",
                    "restore the fixed binding kind and legacy repository path",
                    record_id=binding_id,
                )
            )
            continue
        if expected_kind == "external_exact_file" and (
            binding.get("repo_path") is not None
            or binding.get("locator") != f"external:{binding_id}"
            or binding.get("locator_kind") != "explicit_override"
        ):
            findings.append(
                _finding(
                    "SOURCE_BINDING_INVALID",
                    "manifest",
                    f"external binding {binding_id!r} exposes or misstates its locator",
                    "store only the stable external binding ID; provide local paths at verify time",
                    record_id=binding_id,
                )
            )
        required = binding.get("required") is True
        if not required:
            findings.append(
                _finding(
                    "SOURCE_BINDING_INVALID",
                    "manifest",
                    f"required binding {binding_id!r} is not marked required",
                    "mark every fixed D0 source binding required and regenerate",
                    record_id=binding_id,
                )
            )
        status = binding.get("status")
        if status != "BOUND":
            if required:
                findings.append(
                    _finding(
                        "BINDING_MISSING",
                        "manifest",
                        f"required source binding {binding_id!r} has status {status!r}",
                        "provide and hash the exact source before sealing D0",
                        record_id=binding_id or None,
                    )
                )
            continue
        try:
            raw = _binding_bytes(
                binding,
                repo_root=repo_root,
                legacy_commit=legacy_commit,
                external_bindings=external_bindings,
            )
        except _VerifierResourceLimit as exc:
            findings.append(
                _finding(
                    "RESOURCE_LIMIT_EXCEEDED",
                    "manifest",
                    f"cannot read bound source {binding_id!r}: {exc}",
                    "reduce the bound input to the fixed verifier resource budget",
                    record_id=binding_id or None,
                )
            )
            continue
        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            findings.append(
                _finding(
                    "BINDING_MISSING",
                    "manifest",
                    f"cannot read bound source {binding_id!r}: {exc}",
                    "restore or explicitly override the exact bound source",
                    record_id=binding_id or None,
                )
            )
            continue
        if raw is None and expected_kind == "external_exact_file":
            findings.append(
                _finding(
                    "BINDING_MISSING",
                    "manifest",
                    f"external binding {binding_id!r} requires an explicit --binding override",
                    "rerun with BINDING_ID=/absolute/path for the exact approved bytes",
                    record_id=binding_id,
                )
            )
            continue
        expected_sha = binding.get("sha256")
        if (
            raw is None
            or not isinstance(expected_sha, str)
            or not _SHA256_REF.fullmatch(expected_sha)
            or _sha256(raw) != expected_sha
            or len(raw) != binding.get("byte_length")
        ):
            findings.append(
                _finding(
                    "BINDING_HASH_MISMATCH",
                    "manifest",
                    f"bound source {binding_id!r} no longer matches exact bytes",
                    "regenerate catalog and invalidate prior verification/approval",
                    record_id=binding_id or None,
                )
            )
            continue
        if binding_id == "successor_d0_design":
            missing_anchors = [
                token
                for token in SUCCESSOR_CONSTITUTION_ANCHORS
                if token.encode("utf-8") not in raw
            ]
            if missing_anchors:
                findings.append(
                    _finding(
                        "EVIDENCE_REF_INVALID",
                        "manifest",
                        "successor design is missing constitution anchors: "
                        + ", ".join(missing_anchors),
                        "restore the stable accepted-principle anchors and regenerate",
                        record_id=binding_id,
                    )
                )


def _verify_config_snapshot(
    config_snapshot: object,
    *,
    override: Path | None,
    findings: list[Finding],
) -> None:
    if not isinstance(config_snapshot, dict):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "config_snapshot",
                "config_snapshot must be a typed object",
                "regenerate with OMITTED or one exact file binding",
            )
        )
        return
    mode = config_snapshot.get("mode")
    if mode == "OMITTED":
        if (
            any(
                config_snapshot.get(field) is not None
                for field in ("provided_path", "sha256", "byte_length")
            )
            or config_snapshot.get("locator") is not None
            or config_snapshot.get("locator_kind") != "omitted"
        ):
            findings.append(
                _finding(
                    "MANIFEST_INVALID",
                    "config_snapshot",
                    "OMITTED config snapshot contains exact-file metadata",
                    "regenerate the omitted config binding without stale metadata",
                )
            )
        return
    if mode != "EXACT_FILE":
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "config_snapshot",
                f"unknown config snapshot mode: {mode!r}",
                "use OMITTED or EXACT_FILE",
            )
        )
        return
    if (
        config_snapshot.get("provided_path") is not None
        or config_snapshot.get("locator") != "external:config_snapshot"
        or config_snapshot.get("locator_kind") != "explicit_override"
    ):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "config_snapshot",
                "EXACT_FILE config must use a redacted logical external locator",
                "remove local paths from the manifest and regenerate",
            )
        )
    if override is None:
        findings.append(
            _finding(
                "BINDING_MISSING",
                "config_snapshot",
                "EXACT_FILE config verification requires --config-snapshot",
                "provide the exact external config snapshot path",
            )
        )
        return
    try:
        raw = _read_exact_regular_file(override)
    except _VerifierResourceLimit as exc:
        findings.append(
            _finding(
                "RESOURCE_LIMIT_EXCEEDED",
                "config_snapshot",
                str(exc),
                "reduce the config snapshot to the fixed verifier resource budget",
            )
        )
        return
    except (OSError, ValueError) as exc:
        findings.append(
            _finding(
                "BINDING_MISSING",
                "config_snapshot",
                str(exc),
                "restore the exact regular config snapshot and rerun",
            )
        )
        return
    digest = config_snapshot.get("sha256")
    if (
        not isinstance(digest, str)
        or not _SHA256_REF.fullmatch(digest)
        or digest != _sha256(raw)
        or config_snapshot.get("byte_length") != len(raw)
    ):
        findings.append(
            _finding(
                "BINDING_HASH_MISMATCH",
                "config_snapshot",
                "config snapshot exact bytes differ from manifest",
                "regenerate the D0 bundle and invalidate stale verification",
            )
        )


def _verify_generator_identity(
    identity: object,
    *,
    repo_root: Path,
    findings: list[Finding],
) -> None:
    if not isinstance(identity, dict):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "generator_identity",
                "generator_identity must be a typed object",
                "bind the exact generator module, CLI, and schema set",
            )
        )
        return
    if identity.get("code_identity_version") != "exact-file-set-v1":
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "generator_identity",
                "legacy or unknown generator code identity is not accepted",
                "regenerate with exact-file-set-v1 generator identity",
            )
        )
        return
    declared_files = identity.get("implementation_files")
    if not isinstance(declared_files, list):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "generator_identity",
                "implementation_files must be an ordered exact-file list",
                "bind every fixed generator implementation file",
            )
        )
        return
    declared_paths = [row.get("path") if isinstance(row, dict) else None for row in declared_files]
    if declared_paths != list(_EXPECTED_GENERATOR_IMPLEMENTATION_PATHS):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "generator_identity",
                "generator implementation path set/order differs from the verifier-owned set",
                "restore the complete fixed generator implementation set and regenerate",
            )
        )

    expected_files: list[dict[str, Any]] = []
    expected_sources: dict[str, bytes] = {}
    for relative in _EXPECTED_GENERATOR_IMPLEMENTATION_PATHS:
        try:
            raw = _read_exact_regular_file(repo_root.resolve() / relative)
        except (OSError, ValueError) as exc:
            findings.append(
                _finding(
                    "BINDING_MISSING",
                    "generator_identity",
                    f"cannot read generator implementation {relative}: {exc}",
                    "restore the exact generator implementation file",
                )
            )
            continue
        expected_sources[relative] = raw
        expected_files.append(
            {
                "path": relative,
                "sha256": _sha256(raw),
                "byte_length": len(raw),
            }
        )
    import_errors = _generator_import_closure_errors(expected_sources)
    if import_errors:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "generator_identity",
                "generator import closure escapes exact-file-set-v1: "
                + "; ".join(import_errors[:5]),
                "move every local executable dependency into the verifier-owned file set",
            )
        )
    if declared_files != expected_files:
        findings.append(
            _finding(
                "BINDING_HASH_MISMATCH",
                "generator_identity",
                "generator exact implementation bytes differ from manifest",
                "regenerate the bundle with the current complete generator file set",
            )
        )
    expected_identity_tuples = [
        [row["path"], row["sha256"], row["byte_length"]] for row in expected_files
    ]
    expected_root = _sha256(_canonical_json_bytes(expected_identity_tuples))
    if identity.get("implementation_root_sha256") != expected_root:
        findings.append(
            _finding(
                "BINDING_HASH_MISMATCH",
                "generator_identity",
                "generator path/hash/byte-length tuple root differs from current files",
                "regenerate the exact generator code identity",
            )
        )
    if identity.get("entry_symbol") != (
        "scripts.successor_d0_generation.builder.SuccessorD0Catalog.generate"
    ):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "generator_identity",
                "generator entry_symbol differs from the fixed deep-module seam",
                "restore the canonical SuccessorD0Catalog.generate entry symbol",
            )
        )
    if identity.get("implementation_version") != "d0-catalog-v1":
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "generator_identity",
                "generator implementation_version differs from the v1 contract",
                "restore the declared v1 implementation identity",
            )
        )
    expected_schema_hash = _sha256(
        _canonical_json_bytes({"manifest": MANIFEST_SCHEMA, **ARTIFACT_SCHEMAS})
    )
    if identity.get("schema_set_sha256") != expected_schema_hash:
        findings.append(
            _finding(
                "BINDING_HASH_MISMATCH",
                "generator_identity",
                "generator schema-set hash differs from the verifier-owned v1 contract",
                "align the wire schemas and regenerate",
            )
        )
