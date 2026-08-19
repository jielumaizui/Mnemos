"""Private implementation module for successor_d0_generation.snapshot."""

from __future__ import annotations

from contextlib import contextmanager

from dataclasses import dataclass

from pathlib import Path

from pathlib import PurePosixPath

from typing import Any

from typing import Callable

from typing import Iterator

from typing import Mapping

from typing import Sequence

import ast

import hashlib

import json

import os

import re

import selectors

import stat

import subprocess

import tarfile

import tempfile

import time

from .model import (
    CatalogInputError,
    GIT_ARCHIVE_TIMEOUT_SECONDS,
    GIT_COMMAND_TIMEOUT_SECONDS,
    MAX_EXTERNAL_BINDING_BYTES,
    MAX_SNAPSHOT_ARCHIVE_BYTES,
    MAX_SNAPSHOT_BLOB_BYTES,
    MAX_SNAPSHOT_FILE_COUNT,
    MAX_SNAPSHOT_TOTAL_BYTES,
    REQUIRED_SOURCE_BINDINGS,
    SUCCESSOR_CONSTITUTION_ANCHORS,
    _EXACT_COMMIT,
    _reject_json_constant,
    sha256_bytes,
)


@dataclass(frozen=True)
class _CatalogTreeBlob:
    """One verifier-independent regular blob expected in the Git tree."""

    path: str
    mode: str
    object_id: str
    size: int


_MAX_GIT_STDERR_BYTES = 64 * 1024

_MAX_TREE_LISTING_BYTES = 64 * 1024 * 1024

_MAX_TREE_RECORD_BYTES = 16 * 1024


def _catalog_stream_git(
    repo_root: Path,
    arguments: Sequence[str],
    *,
    label: str,
    timeout_seconds: int,
    max_stdout_bytes: int,
    consume_stdout: Callable[[bytes], object],
) -> int:
    """Run Git while bounding and incrementally consuming both output streams."""

    process = subprocess.Popen(
        ["git", *arguments],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
        process.kill()
        process.wait()
        raise CatalogInputError(f"{label} did not expose bounded pipes")
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
                raise CatalogInputError(f"{label} timed out after {timeout_seconds} seconds")
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
                        raise CatalogInputError(f"{label} exceeds byte limit {max_stdout_bytes}")
                    consume_stdout(chunk)
                elif len(stderr) < _MAX_GIT_STDERR_BYTES:
                    remaining_stderr = _MAX_GIT_STDERR_BYTES - len(stderr)
                    stderr.extend(chunk[:remaining_stderr])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CatalogInputError(f"{label} timed out after {timeout_seconds} seconds")
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise CatalogInputError(f"{label} timed out after {timeout_seconds} seconds") from exc
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
        raise CatalogInputError(detail or f"{label} failed")
    return stdout_bytes


def _git(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CatalogInputError(
            f"git command timed out after {GIT_COMMAND_TIMEOUT_SECONDS} seconds"
        ) from exc
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise CatalogInputError(detail)
    return completed.stdout.strip()


def _catalog_safe_git_path(raw_path: bytes | str, *, source: str) -> PurePosixPath:
    try:
        value = (
            raw_path.decode("utf-8", errors="strict") if isinstance(raw_path, bytes) else raw_path
        )
    except UnicodeDecodeError as exc:
        raise CatalogInputError(f"non-UTF-8 path in {source}") from exc
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
        raise CatalogInputError(f"unsafe or abnormal path in {source}: {value!r}")
    return PurePosixPath(*parts)


def _catalog_tree_inventory(
    repo_root: Path,
    commit: str,
    object_format: str,
) -> tuple[_CatalogTreeBlob, ...]:
    expected_oid_length = {"sha1": 40, "sha256": 64}.get(object_format)
    if expected_oid_length is None:
        raise CatalogInputError(f"unsupported Git object format: {object_format!r}")

    entries: list[_CatalogTreeBlob] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    pending = bytearray()

    def accept_entry(raw_entry: bytes) -> None:
        nonlocal total_bytes
        try:
            header, raw_path = raw_entry.split(b"\t", 1)
            mode_raw, kind_raw, oid_raw, size_raw = header.split()
            mode = mode_raw.decode("ascii")
            kind = kind_raw.decode("ascii")
            object_id = oid_raw.decode("ascii")
            size = int(size_raw.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise CatalogInputError("malformed git ls-tree entry") from exc
        path = _catalog_safe_git_path(raw_path, source="git ls-tree").as_posix()
        if path in seen_paths:
            raise CatalogInputError(f"duplicate path in git tree: {path!r}")
        seen_paths.add(path)
        if kind != "blob" or mode not in {"100644", "100755"}:
            if mode == "120000":
                label = "symlink"
            elif kind == "commit" or mode == "160000":
                label = "gitlink"
            else:
                label = f"unsupported {kind}/{mode}"
            raise CatalogInputError(f"{label} is not allowed in D0 snapshot: {path}")
        if len(object_id) != expected_oid_length or not re.fullmatch(r"[0-9a-f]+", object_id):
            raise CatalogInputError(f"invalid Git blob object ID for {path}")
        if size < 0 or size > MAX_SNAPSHOT_BLOB_BYTES:
            raise CatalogInputError(
                f"snapshot blob exceeds limit {MAX_SNAPSHOT_BLOB_BYTES}: {path} ({size})"
            )
        entries.append(_CatalogTreeBlob(path=path, mode=mode, object_id=object_id, size=size))
        if len(entries) > MAX_SNAPSHOT_FILE_COUNT:
            raise CatalogInputError(f"snapshot file count exceeds limit {MAX_SNAPSHOT_FILE_COUNT}")
        total_bytes += size
        if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
            raise CatalogInputError(f"snapshot total bytes exceed limit {MAX_SNAPSHOT_TOTAL_BYTES}")

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
            raise CatalogInputError(
                f"git ls-tree record exceeds byte limit {_MAX_TREE_RECORD_BYTES}"
            )

    _catalog_stream_git(
        repo_root,
        ["ls-tree", "-r", "-z", "-l", "--full-tree", commit],
        label="git ls-tree",
        timeout_seconds=GIT_COMMAND_TIMEOUT_SECONDS,
        max_stdout_bytes=_MAX_TREE_LISTING_BYTES,
        consume_stdout=consume_listing,
    )
    if pending:
        raise CatalogInputError("git ls-tree output is not NUL terminated")
    return tuple(entries)


def _catalog_blob_oid(path: Path, size: int, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {size}\0".encode("ascii"))
    read_bytes = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            read_bytes += len(chunk)
            if read_bytes > size or read_bytes > MAX_SNAPSHOT_BLOB_BYTES:
                raise CatalogInputError(f"snapshot file changed or exceeded limit: {path}")
            digest.update(chunk)
    if read_bytes != size:
        raise CatalogInputError(f"snapshot file size changed while hashing: {path}")
    return digest.hexdigest()


def _extract_git_archive(
    repo_root: Path,
    commit: str,
    destination: Path,
    expected_entries: Sequence[_CatalogTreeBlob],
    object_format: str,
) -> None:
    archive_path = destination.parent / "snapshot.tar"
    with archive_path.open("xb") as archive_file:
        archive_size = _catalog_stream_git(
            repo_root,
            ["archive", "--format=tar", commit],
            label="git archive",
            timeout_seconds=GIT_ARCHIVE_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_SNAPSHOT_ARCHIVE_BYTES,
            consume_stdout=archive_file.write,
        )
    if archive_path.stat().st_size != archive_size:
        raise CatalogInputError("git archive stream byte count differs from durable file size")

    expected = {entry.path: entry for entry in expected_entries}
    expected_directories = {
        PurePosixPath(entry.path).parents[index].as_posix()
        for entry in expected_entries
        for index in range(len(PurePosixPath(entry.path).parents) - 1)
    }
    actual_paths: set[str] = set()
    actual_total = 0
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                relative = _catalog_safe_git_path(member.name, source="git archive")
                relative_text = relative.as_posix()
                if member.isdir():
                    if relative_text not in expected_directories:
                        raise CatalogInputError(
                            f"unexpected directory in git archive: {relative_text}"
                        )
                    (destination / relative).mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym():
                    raise CatalogInputError(
                        f"symlink is not allowed in git archive: {relative_text}"
                    )
                if member.islnk():
                    raise CatalogInputError(
                        f"hard link is not allowed in git archive: {relative_text}"
                    )
                if not member.isfile():
                    raise CatalogInputError(f"unsupported git archive member: {relative_text}")
                expected_entry = expected.get(relative_text)
                if expected_entry is None:
                    raise CatalogInputError(
                        f"git archive contains path absent from ls-tree snapshot: {relative_text}"
                    )
                if relative_text in actual_paths:
                    raise CatalogInputError(
                        f"duplicate regular file in git archive: {relative_text}"
                    )
                if member.size != expected_entry.size or member.size > MAX_SNAPSHOT_BLOB_BYTES:
                    raise CatalogInputError(
                        f"git archive blob size differs from ls-tree snapshot: {relative_text}"
                    )
                actual_paths.add(relative_text)
                actual_total += member.size
                if (
                    len(actual_paths) > MAX_SNAPSHOT_FILE_COUNT
                    or actual_total > MAX_SNAPSHOT_TOTAL_BYTES
                ):
                    raise CatalogInputError("git archive exceeds snapshot resource limits")
                source = archive.extractfile(member)
                if source is None:
                    raise CatalogInputError(f"unreadable git archive member: {relative_text}")
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > expected_entry.size:
                            raise CatalogInputError(
                                f"git archive member exceeded expected size: {relative_text}"
                            )
                        output.write(chunk)
                if written != expected_entry.size:
                    raise CatalogInputError(f"git archive member was truncated: {relative_text}")
                os.chmod(target, 0o755 if expected_entry.mode == "100755" else 0o644)
    except (OSError, tarfile.TarError) as exc:
        raise CatalogInputError(f"invalid git archive: {exc}") from exc

    missing = sorted(set(expected) - actual_paths)
    if missing:
        preview = ", ".join(missing[:3])
        raise CatalogInputError(
            f"git archive differs from ls-tree snapshot; missing paths: {preview}"
        )
    for relative_text, expected_entry in expected.items():
        target = destination.joinpath(*PurePosixPath(relative_text).parts)
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CatalogInputError(f"snapshot target is not a regular file: {relative_text}")
        if metadata.st_size != expected_entry.size:
            raise CatalogInputError(f"snapshot size mismatch: {relative_text}")
        if bool(metadata.st_mode & 0o111) != (expected_entry.mode == "100755"):
            raise CatalogInputError(f"snapshot mode mismatch: {relative_text}")
        if (
            _catalog_blob_oid(target, expected_entry.size, object_format)
            != expected_entry.object_id
        ):
            raise CatalogInputError(
                f"git archive blob OID differs from ls-tree snapshot: {relative_text}"
            )


@contextmanager
def _archived_snapshot(
    repo_root: Path,
    requested_commit: str,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    root = repo_root.expanduser().resolve()
    if not requested_commit or not _EXACT_COMMIT.fullmatch(requested_commit):
        raise CatalogInputError(
            "--legacy-commit must be a complete lowercase 40- or 64-hex commit object ID"
        )
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise CatalogInputError(f"repo_root must be the Git top-level: {top}")
    resolved = _git(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{requested_commit}^{{commit}}",
    )
    if resolved != requested_commit:
        raise CatalogInputError("--legacy-commit must identify the exact resolved commit object")
    tree_hash = _git(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{resolved}^{{tree}}",
    )
    object_format = _git(root, "rev-parse", "--show-object-format")
    expected_entries = _catalog_tree_inventory(root, resolved, object_format)
    with tempfile.TemporaryDirectory(prefix="mnemos-successor-d0-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir(mode=0o700)
        _extract_git_archive(root, resolved, snapshot, expected_entries, object_format)
        yield snapshot, {
            "commit": resolved,
            "tree": tree_hash,
            "requested_commit": requested_commit,
            "git_object_format": object_format,
            "archive_format": "git-archive-tar+ls-tree-blob-oid-v1",
            "file_count": len(expected_entries),
            "total_blob_bytes": sum(entry.size for entry in expected_entries),
        }


class _CatalogContext:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.findings: list[dict[str, Any]] = []
        self._hashes: dict[str, str | None] = {}

    def path(self, relative: str) -> Path:
        candidate = self.root.joinpath(*PurePosixPath(relative).parts)
        try:
            candidate.resolve(strict=False).relative_to(self.root.resolve())
        except ValueError as exc:
            raise CatalogInputError(f"snapshot path escapes root: {relative}") from exc
        return candidate

    def read_bytes(self, relative: str, *, required: bool = True) -> bytes | None:
        path = self.path(relative)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if required:
                self.finding(
                    "MISSING_SOURCE",
                    f"required snapshot source is missing: {relative}",
                    source_ref=relative,
                )
            return None
        if not stat.S_ISREG(metadata.st_mode):
            if required:
                self.finding(
                    "INVALID_SOURCE",
                    f"snapshot source is not a regular file: {relative}",
                    source_ref=relative,
                )
            return None
        try:
            return path.read_bytes()
        except OSError as exc:
            if required:
                self.finding(
                    "INVALID_SOURCE",
                    f"snapshot source cannot be read: {relative}: {exc}",
                    source_ref=relative,
                )
            return None

    def read_text(self, relative: str, *, required: bool = True) -> str | None:
        payload = self.read_bytes(relative, required=required)
        if payload is None:
            return None
        try:
            return payload.decode("utf-8")
        except UnicodeError:
            if required:
                self.finding(
                    "INVALID_SOURCE",
                    f"snapshot source is not UTF-8: {relative}",
                    source_ref=relative,
                )
            return None

    def load_json(self, relative: str, *, required: bool = True) -> Any:
        text = self.read_text(relative, required=required)
        if text is None:
            return None
        try:
            return json.loads(text, parse_constant=_reject_json_constant)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if required:
                self.finding(
                    "SCHEMA_INVALID",
                    f"invalid JSON source {relative}: {exc}",
                    source_ref=relative,
                )
            return None

    def parse_python(self, relative: str) -> ast.Module | None:
        text = self.read_text(relative)
        if text is None:
            return None
        try:
            return ast.parse(text, filename=relative)
        except SyntaxError as exc:
            self.finding(
                "ENUMERATOR_FAILED",
                f"cannot parse Python source {relative}: {exc}",
                source_ref=relative,
            )
            return None

    def sha256(self, relative: str) -> str | None:
        if relative not in self._hashes:
            payload = self.read_bytes(relative, required=False)
            self._hashes[relative] = sha256_bytes(payload) if payload is not None else None
        return self._hashes[relative]

    def evidence(self, relative: str, *, anchor: str = "") -> dict[str, Any]:
        return {
            "path": relative,
            "anchor": anchor,
            "sha256": self.sha256(relative),
        }

    def finding(
        self,
        code: str,
        message: str,
        *,
        artifact_id: str | None = None,
        record_id: str | None = None,
        source_ref: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        severity: str = "BLOCKING",
        repair_action: str = "classify or repair the exact source, then regenerate",
    ) -> None:
        self.findings.append(
            {
                "code": code,
                "severity": severity,
                "artifact_id": artifact_id,
                "record_id": record_id,
                "source_ref": source_ref,
                "message": message,
                "repair_action": repair_action,
                "evidence": dict(evidence or {}),
            }
        )


def _external_exact_binding(
    *,
    binding_id: str,
    source_role: str,
    path: Path,
    repo_root: Path,
    required_anchor_tokens: Sequence[str] = (),
) -> dict[str, Any]:
    """Bind one caller-selected file without interpreting its contents."""

    provided = path.expanduser()
    resolved = provided if provided.is_absolute() else repo_root / provided
    try:
        metadata = resolved.lstat()
    except FileNotFoundError as exc:
        raise CatalogInputError(
            f"required external source binding is missing: {binding_id}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise CatalogInputError(
            f"required external source must be a non-symlink regular file: {binding_id}"
        )
    if metadata.st_size > MAX_EXTERNAL_BINDING_BYTES:
        raise CatalogInputError(
            f"required external source exceeds {MAX_EXTERNAL_BINDING_BYTES} bytes: {binding_id}"
        )
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise CatalogInputError(
            f"required external source cannot be read: {binding_id}: {exc}"
        ) from exc
    if len(payload) > MAX_EXTERNAL_BINDING_BYTES:
        raise CatalogInputError(
            f"required external source exceeds {MAX_EXTERNAL_BINDING_BYTES} bytes: {binding_id}"
        )
    missing_anchors = [
        token for token in required_anchor_tokens if token.encode("utf-8") not in payload
    ]
    if missing_anchors:
        raise CatalogInputError(
            f"required external source anchors are missing for {binding_id}: "
            + ", ".join(missing_anchors)
        )
    return {
        "binding_id": binding_id,
        "binding_kind": "external_exact_file",
        "source_role": source_role,
        "repo_path": None,
        "locator": f"external:{binding_id}",
        "locator_kind": "explicit_override",
        "required": True,
        "status": "BOUND",
        "sha256": sha256_bytes(payload),
        "byte_length": len(payload),
    }


def _source_bindings(
    context: _CatalogContext,
    *,
    repo_root: Path,
    design_path: Path,
    phase_contract_path: Path,
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    bindings.extend(
        (
            _external_exact_binding(
                binding_id="successor_d0_design",
                source_role="design",
                path=design_path,
                repo_root=repo_root,
                required_anchor_tokens=SUCCESSOR_CONSTITUTION_ANCHORS,
            ),
            _external_exact_binding(
                binding_id="phase0_7_global_engineering_contract",
                source_role="phase_contract",
                path=phase_contract_path,
                repo_root=repo_root,
            ),
        )
    )
    for binding_id, source_role, relative in REQUIRED_SOURCE_BINDINGS:
        payload = context.read_bytes(relative, required=False)
        if payload is None:
            status_value = "MISSING"
            digest = None
            byte_length = None
            context.finding(
                "MISSING_SOURCE_BINDING",
                f"required D0 source binding is missing: {relative}",
                source_ref=relative,
                evidence={"binding_id": binding_id, "source_role": source_role},
            )
        else:
            status_value = "BOUND"
            digest = sha256_bytes(payload)
            byte_length = len(payload)
        bindings.append(
            {
                "binding_id": binding_id,
                "binding_kind": "legacy_repo_file",
                "source_role": source_role,
                "repo_path": relative,
                "locator": relative,
                "locator_kind": "legacy_repo_relative",
                "required": True,
                "status": status_value,
                "sha256": digest,
                "byte_length": byte_length,
            }
        )
    return sorted(bindings, key=lambda item: item["binding_id"])
