"""Shared trusted-user document import policy and helpers."""

from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DOCUMENT_MAX_FILE_SIZE_KEY = "document_process.max_file_size_mb"
DEFAULT_DOCUMENT_MAX_FILE_SIZE_MB = 100

# System temporary paths are blocked for explicit user-document import. macOS resolves
# /tmp through /private/tmp, so both spellings are kept.
BLOCKED_TEMP_PREFIXES = (
    "/tmp/",  # nosec B108: platform tmp-dir prefix allowlist for ingestion blocking
    "/private/tmp/",
    "/var/tmp/",  # nosec B108: platform tmp-dir prefix allowlist for ingestion blocking
    "/private/var/tmp/",
)

_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{24,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[^'\"\s]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(frozen=True)
class TrustedDocumentValidation:
    """Result of validating a user-specified document path."""

    ok: bool
    path: Path | None
    reason: str
    message: str
    size_bytes: int = 0
    max_size_mb: int = DEFAULT_DOCUMENT_MAX_FILE_SIZE_MB
    config_key: str = DOCUMENT_MAX_FILE_SIZE_KEY


def _cfg_get(config: Any, key: str, default: Any) -> Any:
    if config is None:
        from core.config import get_config

        config = get_config()
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


def document_max_file_size_mb(config: Any = None) -> int:
    """Return the effective max trusted-document size in MB."""

    from core.kia.policy import get_shadowed_value

    configured = _cfg_get(config, DOCUMENT_MAX_FILE_SIZE_KEY, DEFAULT_DOCUMENT_MAX_FILE_SIZE_MB)
    value = get_shadowed_value(DOCUMENT_MAX_FILE_SIZE_KEY, configured)
    if value is None:
        value = DEFAULT_DOCUMENT_MAX_FILE_SIZE_MB
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_DOCUMENT_MAX_FILE_SIZE_MB


def validate_trusted_user_document(
    file_path: Path | str,
    *,
    config: Any = None,
    allow_directory: bool = False,
    blocked_temp_prefixes: tuple[str, ...] = BLOCKED_TEMP_PREFIXES,
) -> TrustedDocumentValidation:
    """Apply the canonical path and size gate for trusted user documents."""

    path = Path(file_path).expanduser()
    max_size_mb = document_max_file_size_mb(config)
    if not path.exists():
        return _reject("文件不存在", path, f"文件不存在: {path}", max_size_mb)
    if path.is_symlink():
        return _reject("拒绝摄入符号链接", path, f"拒绝摄入符号链接（安全风险）: {path}", max_size_mb)
    try:
        mode = path.stat().st_mode
    except (OSError, IOError):
        return _reject("无法获取文件状态", path, f"无法读取文件状态: {path}", max_size_mb)

    if stat.S_ISDIR(mode):
        if allow_directory:
            return TrustedDocumentValidation(True, path, "", "", max_size_mb=max_size_mb)
        return _reject("路径为目录，请使用 ingest_directory", path, f"该路径是目录，请使用目录导入: {path}", max_size_mb)
    if not stat.S_ISREG(mode):
        return _reject(
            "拒绝摄入非普通文件（设备/管道/socket 等）",
            path,
            f"拒绝摄入特殊文件（设备/管道/socket）: {path}",
            max_size_mb,
        )

    try:
        abs_path = path.resolve(strict=True)
    except (OSError, IOError):
        return _reject(
            "无法解析路径（可能已被删除或无权限）",
            path,
            f"无法解析路径（可能已被删除或无权限）: {path}",
            max_size_mb,
        )

    path_str = str(abs_path).lower()
    if any(path_str.startswith(prefix.lower()) for prefix in blocked_temp_prefixes):
        return _reject("拒绝摄入系统临时目录文件", abs_path, f"拒绝摄入系统临时目录文件: {abs_path}", max_size_mb)

    try:
        raw_vault_value = getattr(config, "obsidian_vault_path", None)
        if raw_vault_value is None:
            raw_vault_value = config.get("vaults.raw.path")
        raw_vault = Path(raw_vault_value).resolve() if raw_vault_value else None
        if raw_vault is not None and abs_path.is_relative_to(raw_vault):
            return _reject("拒绝摄入 L1 raw vault 自身文件", abs_path, f"拒绝摄入 Mnemos raw vault 自身文件: {abs_path}", max_size_mb)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        return _reject("无法读取 raw vault 配置，默认拒绝摄入", abs_path, f"无法读取 Mnemos 配置，默认拒绝摄入: {abs_path}", max_size_mb)

    try:
        size_bytes = abs_path.stat().st_size
    except (OSError, IOError):
        return _reject("无法读取文件大小", abs_path, f"无法读取文件大小: {abs_path}", max_size_mb)

    max_size_bytes = max_size_mb * 1024 * 1024
    if size_bytes > max_size_bytes:
        size_mb = size_bytes / 1024 / 1024
        return _reject(
            f"文件过大（超过 {max_size_mb}MB）",
            abs_path,
            (
                f"文件过大 ({size_mb:.1f}MB)，超过 {max_size_mb}MB 限制；"
                f"配置项 {DOCUMENT_MAX_FILE_SIZE_KEY}"
            ),
            max_size_mb,
            size_bytes=size_bytes,
        )

    return TrustedDocumentValidation(
        ok=True,
        path=abs_path,
        reason="",
        message="",
        size_bytes=size_bytes,
        max_size_mb=max_size_mb,
    )


def _reject(
    reason: str,
    path: Path,
    message: str,
    max_size_mb: int,
    *,
    size_bytes: int = 0,
) -> TrustedDocumentValidation:
    return TrustedDocumentValidation(
        ok=False,
        path=path,
        reason=reason,
        message=message,
        size_bytes=size_bytes,
        max_size_mb=max_size_mb,
    )


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Compute a stable content hash for import result and ledger references."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_trusted_document_privacy(path: Path, *, sample_bytes: int = 256 * 1024) -> dict[str, Any]:
    """Run a conservative local privacy scan over a small text sample."""

    signals: list[str] = []
    try:
        with Path(path).open("rb") as handle:
            raw_sample = handle.read(sample_bytes)
        sample = raw_sample.decode("utf-8", errors="ignore")
    except (OSError, IOError, UnicodeDecodeError):
        raw_sample = b""
        sample = ""
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        if pattern.search(sample):
            signals.append(pattern.pattern)
    return {
        "schema_version": "mnemos.trusted_document_privacy_scan.v1",
        "status": "needs_review" if signals else "ok",
        "signals": signals,
        "sample_bytes": len(raw_sample),
        "policy": "trusted_user_document",
    }
