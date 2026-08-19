"""Shared public types for durable filesystem operations."""


class DurableIOError(OSError):
    """A file or directory could not be durably synchronized."""
