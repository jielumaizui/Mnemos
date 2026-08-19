from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat

import pytest

import core.ops.durable_io as durable_io_module
from core.ops.durable_io import (
    DurableIOError,
    SecureImmutablePublishReceipt,
    ensure_private_directory,
    inspect_path_kind,
    owned_sqlite_connection_pair,
    physical_scope_signature,
    secure_atomic_write_text,
    secure_cleanup_created_tree,
    secure_create_directory,
    secure_directory_tree_inventory,
    secure_publish_immutable_text,
    secure_remove_directory_tree,
    secure_remove_regular_file,
    validate_secure_created_file_receipts,
    validate_private_sqlite_copy,
)


def test_path_inspection_distinguishes_missing_from_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.db"
    assert inspect_path_kind(target) == "missing"

    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(DurableIOError, match="durable_path_inspection_failed"):
        inspect_path_kind(target)


def test_path_inspection_never_follows_a_leaf_symlink(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    target.write_bytes(b"authoritative-state")
    link = tmp_path / "state-link.db"
    link.symlink_to(target)

    assert inspect_path_kind(target) == "file"
    assert inspect_path_kind(link) == "other"


def test_bounded_file_signature_ignores_metadata_only_timestamp_churn(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sqlite-lock-shm"
    target.write_bytes(b"unchanged-lock-bytes")
    before = physical_scope_signature((target,))

    metadata = target.stat()
    os.utime(
        target,
        ns=(metadata.st_atime_ns + 1_000_000, metadata.st_mtime_ns + 1_000_000),
    )

    after = physical_scope_signature((target,))
    assert before == after
    assert before["comparison_contract"] == (
        "stable-nofollow-content-and-inventory-v3"
    )


def test_bounded_file_signature_detects_same_length_content_change(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sqlite-lock-shm"
    target.write_bytes(b"before")
    before = physical_scope_signature((target,))

    target.write_bytes(b"after!")

    assert physical_scope_signature((target,)) != before


def test_file_signature_never_follows_a_leaf_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.db"
    target.write_bytes(b"reviewed")
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"foreign!")
    real_open = durable_io_module.os.open
    injected = {"done": False}

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == target and not injected["done"]:
            injected["done"] = True
            target.unlink()
            target.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(durable_io_module.os, "open", replace_before_open)

    with pytest.raises(DurableIOError, match="durable_signature_file_unavailable"):
        physical_scope_signature((target,))

    assert injected["done"] is True
    assert target.is_symlink()
    assert outside.read_bytes() == b"foreign!"


def test_unhashed_file_signature_still_anchors_the_exact_leaf_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "large.db"
    target.write_bytes(b"reviewed")
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"foreign")
    detached = tmp_path / "large.detached.db"
    real_open = durable_io_module.os.open
    injected = {"done": False}

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == target and not injected["done"]:
            injected["done"] = True
            target.replace(detached)
            replacement.replace(target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(durable_io_module.os, "open", replace_before_open)

    with pytest.raises(DurableIOError, match="durable_signature_file_changed"):
        physical_scope_signature((target,), hash_max_bytes=0)

    assert injected["done"] is True
    assert target.read_bytes() == b"foreign"


def test_file_signature_binds_device_and_inode(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    target.write_bytes(b"state")

    entry = physical_scope_signature((target,))["entries"][0]

    metadata = target.lstat()
    assert entry["device"] == metadata.st_dev
    assert entry["inode"] == metadata.st_ino


def test_private_sqlite_validation_rejects_leaf_replacement_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "backup.sqlite"
    outside = tmp_path / "outside.sqlite"
    detached = tmp_path / "backup.detached.sqlite"
    for path, value in ((target, "reviewed"), (outside, "foreign")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE sentinel(value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES (?)", (value,))
    outside_before = outside.read_bytes()
    real_connect = durable_io_module.sqlite3.connect
    injected = {"done": False}

    def replace_before_connect(database, *args, **kwargs):
        if str(target) in str(database) and not injected["done"]:
            injected["done"] = True
            target.replace(detached)
            target.symlink_to(outside)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(durable_io_module.sqlite3, "connect", replace_before_connect)

    with pytest.raises(DurableIOError, match="private_sqlite_copy_changed"):
        validate_private_sqlite_copy(target)

    assert injected["done"] is True
    assert outside.read_bytes() == outside_before


def test_signature_detects_new_inventory_child(tmp_path: Path) -> None:
    private_dir = tmp_path / "backups"
    private_dir.mkdir()
    before = physical_scope_signature((), inventory_directory=private_dir)

    (private_dir / "unexpected-write").write_bytes(b"drift")

    assert physical_scope_signature((), inventory_directory=private_dir) != before


def test_inventory_signature_rejects_a_child_created_during_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_dir = tmp_path / "backups"
    private_dir.mkdir()
    original_entry = durable_io_module._physical_entry  # noqa: SLF001
    injected = False

    def create_after_directory_entry(path: Path, *, hash_max_bytes: int):
        nonlocal injected
        entry = original_entry(path, hash_max_bytes=hash_max_bytes)
        if path == private_dir and not injected:
            injected = True
            (private_dir / "late-child").write_bytes(b"drift")
        return entry

    monkeypatch.setattr(
        durable_io_module,
        "_physical_entry",
        create_after_directory_entry,
    )

    with pytest.raises(
        DurableIOError,
        match="durable_inventory_directory_changed",
    ):
        physical_scope_signature((), inventory_directory=private_dir)

    assert injected is True


def test_inventory_signature_rejects_a_child_created_during_verification_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_dir = tmp_path / "backups"
    private_dir.mkdir()
    original_entry = durable_io_module._physical_entry  # noqa: SLF001
    directory_entry_reads = 0

    def create_during_verification(path: Path, *, hash_max_bytes: int):
        nonlocal directory_entry_reads
        entry = original_entry(path, hash_max_bytes=hash_max_bytes)
        if path == private_dir:
            directory_entry_reads += 1
            if directory_entry_reads == 2:
                (private_dir / "late-verification-child").write_bytes(b"drift")
        return entry

    monkeypatch.setattr(
        durable_io_module,
        "_physical_entry",
        create_during_verification,
    )

    with pytest.raises(
        DurableIOError,
        match="durable_inventory_directory_changed",
    ):
        physical_scope_signature((), inventory_directory=private_dir)

    assert directory_entry_reads == 2


def test_owned_sqlite_pair_closes_source_when_destination_open_fails() -> None:
    closed: list[str] = []

    class Source:
        def close(self) -> None:
            closed.append("source")

    failure = sqlite3.OperationalError("destination unavailable")

    with pytest.raises(sqlite3.OperationalError, match="destination unavailable"):
        with owned_sqlite_connection_pair(
            lambda: Source(),  # type: ignore[arg-type,return-value]
            lambda: (_ for _ in ()).throw(failure),
        ):
            raise AssertionError("unreachable")

    assert closed == ["source"]


def test_owned_sqlite_pair_closes_both_connections_after_body_failure() -> None:
    closed: list[str] = []

    class Connection:
        def __init__(self, label: str) -> None:
            self.label = label

        def close(self) -> None:
            closed.append(self.label)

    with pytest.raises(RuntimeError, match="body failed"):
        with owned_sqlite_connection_pair(
            lambda: Connection("source"),  # type: ignore[arg-type,return-value]
            lambda: Connection("destination"),  # type: ignore[arg-type,return-value]
        ):
            raise RuntimeError("body failed")

    assert closed == ["destination", "source"]


def test_secure_atomic_write_never_follows_leaf_or_parent_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    (root / "state.json").symlink_to(outside)

    with pytest.raises(DurableIOError, match="durable_target_unsafe"):
        secure_atomic_write_text(root, "state.json", '{"changed":true}')

    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert (root / "state.json").is_symlink()

    redirected_root = tmp_path / "redirected-root"
    redirected_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(DurableIOError, match="durable_directory_path_unsafe"):
        secure_atomic_write_text(redirected_root, "other.json", "{}")
    assert not (root / "other.json").exists()


def test_secure_remove_regular_file_never_follows_leaf_or_parent_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-remove"
    root.mkdir()
    outside = tmp_path / "outside-remove.txt"
    outside.write_text("sentinel", encoding="utf-8")
    (root / "payload.json").symlink_to(outside)

    with pytest.raises(DurableIOError, match="durable_target_not_regular"):
        secure_remove_regular_file(root, "payload.json")
    assert outside.read_text(encoding="utf-8") == "sentinel"

    owned = root / "owned.json"
    owned.write_text("owned", encoding="utf-8")
    redirected_root = tmp_path / "redirected-remove-root"
    redirected_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(DurableIOError, match="durable_directory_path_unsafe"):
        secure_remove_regular_file(redirected_root, "owned.json")
    assert owned.read_text(encoding="utf-8") == "owned"


def test_secure_immutable_publish_is_idempotent_but_rejects_collision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "immutable"
    first = secure_publish_immutable_text(root, "session/artifact.md", "exact")
    replay = secure_publish_immutable_text(root, "session/artifact.md", "exact")

    assert first == replay
    assert first.read_text(encoding="utf-8") == "exact"
    with pytest.raises(DurableIOError, match="durable_immutable_collision"):
        secure_publish_immutable_text(root, "session/artifact.md", "drift")
    assert first.read_text(encoding="utf-8") == "exact"


def test_secure_immutable_publish_receipt_binds_created_and_replay_preimage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "immutable-receipt"
    first = secure_publish_immutable_text(
        root,
        "session/artifact.md",
        "exact",
        return_receipt=True,
    )
    replay = secure_publish_immutable_text(
        root,
        "session/artifact.md",
        "exact",
        return_receipt=True,
    )

    assert isinstance(first, SecureImmutablePublishReceipt)
    assert isinstance(replay, SecureImmutablePublishReceipt)
    assert first.created is True
    assert replay.created is False
    assert replay.path == first.path
    assert replay.preimage == first.preimage
    assert first.preimage["sha256"] == hashlib.sha256(b"exact").hexdigest()


def test_secure_immutable_receipt_never_rebinds_to_publish_race_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "immutable-race"
    target = root / "artifact.md"
    real_fsync = durable_io_module.os.fsync
    injected = {"done": False}

    def replace_after_directory_sync(descriptor: int) -> None:
        real_fsync(descriptor)
        metadata = os.fstat(descriptor)
        if not injected["done"] and stat.S_ISDIR(metadata.st_mode) and target.exists():
            injected["done"] = True
            target.unlink()
            target.write_bytes(b"foreign")

    monkeypatch.setattr(durable_io_module.os, "fsync", replace_after_directory_sync)
    with pytest.raises(DurableIOError, match="durable_target_preimage_changed"):
        secure_publish_immutable_text(
            root,
            target.name,
            "owned",
            return_receipt=True,
        )

    assert target.read_bytes() == b"foreign"


def test_secure_remove_regular_file_preserves_replacement_of_owned_publish(
    tmp_path: Path,
) -> None:
    root = tmp_path / "immutable-cleanup"
    receipt = secure_publish_immutable_text(
        root,
        "artifact.md",
        "owned",
        return_receipt=True,
    )
    assert isinstance(receipt, SecureImmutablePublishReceipt)
    receipt.path.unlink()
    receipt.path.write_bytes(b"foreign")

    with pytest.raises(DurableIOError, match="durable_target_preimage_changed"):
        secure_remove_regular_file(
            root,
            receipt.path.name,
            expected_preimage=receipt.preimage,
        )

    assert receipt.path.read_bytes() == b"foreign"


@pytest.mark.parametrize("bind_exact_preimage", [False, True])
def test_secure_remove_regular_file_never_unlinks_concurrent_public_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bind_exact_preimage: bool,
) -> None:
    root = tmp_path / "immutable-cleanup-race"
    receipt = secure_publish_immutable_text(
        root,
        "artifact.md",
        "owned",
        return_receipt=True,
    )
    assert isinstance(receipt, SecureImmutablePublishReceipt)
    real_rename = os.rename
    injected = {"done": False}

    def recreate_public_name_after_quarantine(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        real_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if source == receipt.path.name and target.endswith(".remove"):
            injected["done"] = True
            descriptor = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=src_dir_fd,
            )
            try:
                os.write(descriptor, b"foreign")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    monkeypatch.setattr(
        durable_io_module.os,
        "rename",
        recreate_public_name_after_quarantine,
    )

    assert (
        secure_remove_regular_file(
            root,
            receipt.path.name,
            expected_preimage=receipt.preimage if bind_exact_preimage else None,
        )
        is True
    )
    assert injected["done"] is True
    assert receipt.path.read_bytes() == b"foreign"


def test_created_file_receipt_validation_rejects_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "created-file-validation"
    receipt = secure_publish_immutable_text(
        root,
        "artifact.md",
        "owned",
        return_receipt=True,
    )
    assert isinstance(receipt, SecureImmutablePublishReceipt)
    receipt.path.unlink()
    receipt.path.write_bytes(b"foreign")

    with pytest.raises(DurableIOError, match="durable_target_preimage_changed"):
        validate_secure_created_file_receipts(
            root,
            {receipt.path.name: receipt.preimage},
        )

    assert receipt.path.read_bytes() == b"foreign"


def test_private_directory_creation_rejects_symlink_component_and_collision(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DurableIOError, match="durable_directory_path_unsafe"):
        ensure_private_directory(redirected / "backups")
    assert not (outside / "backups").exists()

    root = ensure_private_directory(tmp_path / "private")
    creation = secure_create_directory(root, "generation")
    assert creation["mode"] == 0o700
    assert stat.S_IMODE((root / "generation").stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        secure_create_directory(root, "generation")


def test_created_tree_cleanup_removes_only_receipt_bound_entries(
    tmp_path: Path,
) -> None:
    root = ensure_private_directory(tmp_path / "cleanup-root")
    leaf_preimage = secure_create_directory(root, "generation")
    nested_preimage = secure_create_directory(root, "generation/nested")
    receipt = secure_publish_immutable_text(
        root,
        "generation/nested/owned.txt",
        "owned",
        return_receipt=True,
    )
    assert isinstance(receipt, SecureImmutablePublishReceipt)
    foreign = root / "generation" / "foreign.txt"
    foreign.write_bytes(b"foreign")

    result = secure_cleanup_created_tree(
        root,
        created_files={
            "generation/nested/owned.txt": receipt.preimage,
        },
        created_directories={
            "generation": leaf_preimage,
            "generation/nested": nested_preimage,
        },
    )

    assert not receipt.path.exists()
    assert not (root / "generation" / "nested").exists()
    assert foreign.read_bytes() == b"foreign"
    assert result["preserved_directories"] == ["generation"]

    foreign.unlink()
    secure_cleanup_created_tree(
        root,
        created_files={},
        created_directories={"generation": leaf_preimage},
    )
    assert not (root / "generation").exists()


def test_secure_directory_delete_requires_unchanged_nofollow_preimage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    candidate = root / "session"
    candidate.mkdir(parents=True)
    (candidate / "turn.md").write_text("first", encoding="utf-8")
    inventory = secure_directory_tree_inventory(root, "session")

    (candidate / "late.md").write_text("late", encoding="utf-8")
    with pytest.raises(DurableIOError, match="durable_directory_preimage_changed"):
        secure_remove_directory_tree(
            root,
            "session",
            expected_inventory_sha256=str(inventory["inventory_sha256"]),
        )
    assert candidate.is_dir()
    assert (candidate / "turn.md").read_text(encoding="utf-8") == "first"

    current = secure_directory_tree_inventory(root, "session")
    file_count, removed_bytes = secure_remove_directory_tree(
        root,
        "session",
        expected_inventory_sha256=str(current["inventory_sha256"]),
    )
    assert file_count == 2
    assert removed_bytes == len("first") + len("late")
    assert not candidate.exists()


def test_secure_directory_inventory_binds_exact_file_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    candidate = root / "session"
    candidate.mkdir(parents=True)
    target = candidate / "turn.md"
    target.write_bytes(b"first")
    original_metadata = target.stat()
    inventory = secure_directory_tree_inventory(root, "session")

    assert inventory["schema_version"] == "mnemos.secure_directory_tree_inventory.v2"
    assert inventory["entries"] == [
        {
            "path": "turn.md",
            "device": original_metadata.st_dev,
            "inode": original_metadata.st_ino,
            "mode": stat.S_IMODE(original_metadata.st_mode),
            "mtime_ns": original_metadata.st_mtime_ns,
            "ctime_ns": original_metadata.st_ctime_ns,
            "kind": "file",
            "size": 5,
            "sha256": hashlib.sha256(b"first").hexdigest(),
        }
    ]

    target.write_bytes(b"other")
    os.utime(
        target,
        ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
    )
    changed = secure_directory_tree_inventory(root, "session")

    assert changed["inventory_sha256"] != inventory["inventory_sha256"]
    assert changed["entries"][0]["sha256"] == hashlib.sha256(b"other").hexdigest()


def test_secure_directory_delete_does_not_remove_replacement_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    candidate = root / "session"
    displaced = root / "displaced"
    candidate.mkdir(parents=True)
    (candidate / "turn.md").write_text("planned", encoding="utf-8")
    inventory = secure_directory_tree_inventory(root, "session")
    real_remove = durable_io_module._remove_directory_contents

    def replace_root(directory_fd: int, **kwargs) -> None:
        candidate.rename(displaced)
        candidate.mkdir()
        real_remove(directory_fd, **kwargs)

    monkeypatch.setattr(
        durable_io_module,
        "_remove_directory_contents",
        replace_root,
    )
    with pytest.raises(DurableIOError, match="durable_directory_target_changed"):
        secure_remove_directory_tree(
            root,
            "session",
            expected_inventory_sha256=str(inventory["inventory_sha256"]),
        )

    assert candidate.is_dir()
    assert displaced.is_dir()


def test_secure_directory_delete_does_not_remove_replacement_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    candidate = root / "session"
    child = candidate / "nested"
    displaced = root / "displaced-nested"
    child.mkdir(parents=True)
    (child / "planned.md").write_text("planned", encoding="utf-8")
    inventory = secure_directory_tree_inventory(root, "session")
    real_remove = durable_io_module._remove_directory_contents

    def replace_child(directory_fd: int, **kwargs) -> None:
        child.rename(displaced)
        child.mkdir()
        real_remove(directory_fd, **kwargs)

    monkeypatch.setattr(
        durable_io_module,
        "_remove_directory_contents",
        replace_child,
    )
    with pytest.raises(DurableIOError, match="durable_directory_delete_changed"):
        secure_remove_directory_tree(
            root,
            "session",
            expected_inventory_sha256=str(inventory["inventory_sha256"]),
        )

    assert child.is_dir()
    assert (displaced / "planned.md").read_text(encoding="utf-8") == "planned"


def test_secure_atomic_write_never_exposes_partial_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "atomic"
    target = secure_atomic_write_text(root, "state.json", "old")
    real_write = durable_io_module.os.write
    injected = {"done": False}

    def short_write(descriptor: int, content: bytes) -> int:
        if not injected["done"]:
            injected["done"] = True
            return 0
        return real_write(descriptor, content)

    monkeypatch.setattr(durable_io_module.os, "write", short_write)
    with pytest.raises(DurableIOError, match="durable_write_incomplete"):
        secure_atomic_write_text(root, "state.json", "new-content")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(root.glob(".*.tmp")) == []
