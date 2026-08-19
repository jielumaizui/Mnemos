"""Compatibility exports for the process-wide stable regular-file reader."""

from core.ops.durable_io import (
    canonical_native_path,
    copy_native_file_to_descriptor,
    open_native_binary,
    open_native_text,
    read_native_bytes,
    read_native_bytes_with_metadata,
)

__all__ = [
    "canonical_native_path",
    "copy_native_file_to_descriptor",
    "open_native_binary",
    "open_native_text",
    "read_native_bytes",
    "read_native_bytes_with_metadata",
]
