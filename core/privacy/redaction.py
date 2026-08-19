"""Redaction helpers for shareable diagnostic output."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_PREFIXES = ("env:", "keyring:", "keyref:")
URL_RE = re.compile(r"https?://[^\s\"'<>),;]+")
KEY_SOURCE_RE = re.compile(r"\b(env|keyring|keyref):[A-Za-z0-9_.:/@-]+")
WINDOWS_USER_RE = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"'<>),;]+(?:\\[^\s\"'<>),;]+)*")
POSIX_USER_RE = re.compile(r"(?<![\w.-])/(?:Users|home)/[^/\s\"'<>),;]+(?:/[^\s\"'<>),;]+)*")
POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w<>.:\-*])/(?!/)[A-Za-z0-9._~+-][^\s\"'<>),;]*"
)

PATH_KEY_HINTS = (
    "path",
    "dir",
    "file",
    "artifact",
    "vault",
    "database",
    "db",
    "backup",
)
URL_KEY_HINTS = ("url", "base_url", "api_url")


def redact_url(value: Any) -> str:
    """Redact a URL host while keeping scheme and endpoint shape."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        path = parsed.path.rstrip("/") or ""
        return f"{parsed.scheme}://****{path}"
    return "<URL>"


def redact_key_source(value: Any) -> str:
    """Redact env/keyring/keyref source identifiers."""
    text = str(value or "")
    for prefix in REFERENCE_PREFIXES:
        if text.startswith(prefix):
            return f"{prefix}****"
    return text


def _replace_root(text: str, root: Path, label: str) -> str:
    try:
        root_text = str(root.expanduser().resolve())
    except OSError:
        root_text = str(root.expanduser())
    if text == root_text:
        return label
    if text.startswith(root_text + "/"):
        return label + text[len(root_text):]
    return text


def redact_path(value: Any) -> str:
    """Redact local absolute paths while preserving useful relative context."""
    text = str(value or "")
    if not text:
        return ""
    if text.startswith("~"):
        return "<HOME>" + text[1:]

    text = _replace_root(text, PROJECT_ROOT, "<REPO>")
    text = _replace_root(text, Path.home(), "<HOME>")
    if text.startswith(("<REPO>", "<HOME>")):
        return text

    windows_match = re.match(r"(?i)^([A-Z]:\\Users\\)[^\\]+(.*)$", text)
    if windows_match:
        return "<HOME>" + windows_match.group(2).replace("\\", "/")

    posix_match = re.match(r"^/(Users|home)/[^/]+(.*)$", text)
    if posix_match:
        return "<HOME>" + (posix_match.group(2) or "")

    if Path(text).is_absolute():
        return f"<PATH>/{Path(text).name}"
    return text


def redact_text(value: Any) -> str:
    """Redact URLs, key references and local user paths inside free text."""
    text = str(value or "")
    if not text:
        return ""
    text = text.replace(str(PROJECT_ROOT), "<REPO>")
    text = text.replace(str(Path.home()), "<HOME>")
    text = URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    text = KEY_SOURCE_RE.sub(lambda match: redact_key_source(match.group(0)), text)
    text = WINDOWS_USER_RE.sub(lambda match: redact_path(match.group(0)), text)
    text = POSIX_USER_RE.sub(lambda match: redact_path(match.group(0)), text)
    text = POSIX_ABSOLUTE_PATH_RE.sub(lambda match: redact_path(match.group(0)), text)
    return text


def _looks_like_url_key(key: str) -> bool:
    lowered = key.lower()
    return lowered == "endpoint" or any(hint in lowered for hint in URL_KEY_HINTS)


def _looks_like_path_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in PATH_KEY_HINTS)


def redact_sensitive_data(value: Any, *, key: str = "") -> Any:
    """Recursively redact shareable diagnostic payloads."""
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_sensitive_data(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, tuple):
        return [redact_sensitive_data(item, key=key) for item in value]
    if isinstance(value, list):
        return [redact_sensitive_data(item, key=key) for item in value]
    if isinstance(value, Path):
        return redact_path(value)
    if isinstance(value, str):
        lowered = key.lower()
        if "key_source" in lowered or (
            lowered == "source" and value.startswith(REFERENCE_PREFIXES)
        ):
            return redact_key_source(value)
        if _looks_like_url_key(key):
            return redact_url(value) if value else ""
        if _looks_like_path_key(key):
            return redact_text(redact_path(value)) if value else ""
        return redact_text(value)
    return value
