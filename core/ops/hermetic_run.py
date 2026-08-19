"""Run-scoped ownership boundary for tests, gates, and diagnostic probes.

The environment is intentionally small: inherited process state is allowlisted,
while every mutable filesystem location is replaced by a path owned by one run.
The manifest is both human evidence and the machine contract passed to children.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from core.runtime_environment import environment_snapshot
from core.utils import atomic_write_text, load_json_value

SCHEMA_VERSION = "mnemos.hermetic_run_environment.v1"
PROFILES = frozenset({"isolated"})
MANIFEST_INTEGRITY_SCHEME = "sha256-v1"

_INHERITED_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "COMSPEC",
        "WINDIR",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "TZ",
        "TERM",
        "COLORTERM",
        "COLUMNS",
        "LINES",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "PYTHONWARNINGS",
        "CI",
        "GITHUB_ACTIONS",
    }
)

_CREDENTIAL_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "SILICONFLOW_API_KEY",
        "DASHSCOPE_API_KEY",
        "MOONSHOT_API_KEY",
        "KIMI_API_KEY",
    }
)

_OWNED_PATH_ENV = (
    "HOME",
    "USERPROFILE",
    "MNEMOS_DIR",
    "MNEMOS_DATABASE_DIR",
    "MNEMOS_WIKI_DIR",
    "MNEMOS_OBSIDIAN_CONFIG_PATH",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPYCACHEPREFIX",
    "MNEMOS_RUN_ARTIFACTS_DIR",
)

_PROCESS_RUN_BINDING: tuple[Path, Path, str] | None = None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _manifest_integrity(payload: Mapping[str, object]) -> str:
    """Return a corruption checksum, never a child-authentication claim."""

    unsigned = dict(payload)
    unsigned.pop("integrity", None)
    return _sha256_text(_canonical_json(unsigned))


def verify_environment_manifest(
    payload: Mapping[str, object],
    environment: Mapping[str, str],
) -> bool:
    """Verify manifest structure, checksum, and agreement with the child environment.

    This is an integrity check only.  A child process can alter its own
    environment, so callers must not use this function as authorization to
    inspect paths outside the run-owned sandbox.
    """

    inherited_keys = payload.get("inherited_environment_keys")
    owned_paths = payload.get("owned_paths")
    integrity = payload.get("integrity")
    if (
        not isinstance(inherited_keys, list)
        or not all(isinstance(key, str) and key for key in inherited_keys)
        or inherited_keys != sorted(set(inherited_keys))
        or not isinstance(owned_paths, dict)
        or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in owned_paths.items()
        )
        or not isinstance(integrity, dict)
        or integrity.get("scheme") != MANIFEST_INTEGRITY_SCHEME
    ):
        return False
    digest = integrity.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    reconstructed: dict[str, str] = {}
    for key in inherited_keys:
        value = environment.get(key)
        if value is None:
            return False
        reconstructed[key] = value
    reconstructed.update(
        {
            key: value
            for key, value in owned_paths.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    )
    controls = {
        "MNEMOS_RUN_ROOT": payload.get("sandbox_root"),
        "MNEMOS_RUN_PROFILE": payload.get("profile"),
        "MNEMOS_RUN_ENVIRONMENT_MANIFEST": payload.get("manifest_path"),
    }
    if not all(isinstance(value, str) and value for value in controls.values()):
        return False
    reconstructed.update({key: str(value) for key, value in controls.items()})
    expected_environment_hash = _sha256_text(_canonical_json(reconstructed))
    if not hmac.compare_digest(
        expected_environment_hash,
        str(payload.get("environment_hash", "")),
    ) or not hmac.compare_digest(
        expected_environment_hash,
        environment.get("MNEMOS_RUN_ENVIRONMENT_HASH", ""),
    ):
        return False
    return hmac.compare_digest(_manifest_integrity(payload), digest)


def bind_process_run_environment(environment: Mapping[str, str]) -> Path:
    """Bind this process once to the HRE established before test collection.

    A test may mutate its environment later, but it cannot legitimately rebind
    the process to a different self-declared root. This is the temporal trust
    anchor used only for sandbox-confined test readers.
    """

    global _PROCESS_RUN_BINDING

    root_value = environment.get("MNEMOS_RUN_ROOT", "")
    manifest_value = environment.get("MNEMOS_RUN_ENVIRONMENT_MANIFEST", "")
    environment_hash = environment.get("MNEMOS_RUN_ENVIRONMENT_HASH", "")
    if (
        environment.get("MNEMOS_RUN_PROFILE") != "isolated"
        or not root_value
        or not manifest_value
        or len(environment_hash) != 64
    ):
        raise RuntimeError("cannot bind an incomplete hermetic run environment")
    raw_root = Path(root_value)
    raw_manifest = Path(manifest_value)
    if raw_root.is_symlink() or raw_manifest.is_symlink():
        raise RuntimeError("cannot bind a symlinked hermetic run identity")
    root = raw_root.expanduser().resolve(strict=False)
    manifest = raw_manifest.expanduser().resolve(strict=False)
    if (
        not root.is_dir()
        or manifest != root / "artifacts" / "environment-manifest.json"
        or not manifest.is_file()
    ):
        raise RuntimeError("cannot bind an invalid hermetic run root or manifest")
    try:
        payload = load_json_value(manifest)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot bind an unreadable hermetic run manifest") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("profile") != "isolated"
        or Path(str(payload.get("sandbox_root", ""))).expanduser().resolve(strict=False) != root
        or Path(str(payload.get("manifest_path", ""))).expanduser().resolve(strict=False)
        != manifest
        or payload.get("environment_hash") != environment_hash
        or payload.get("outside_write_count") != 0
        or payload.get("formal_state_diff") != []
        or not verify_environment_manifest(payload, environment)
    ):
        raise RuntimeError("cannot bind an invalid hermetic run manifest")
    owned_paths = payload.get("owned_paths")
    if not isinstance(owned_paths, dict):
        raise RuntimeError("cannot bind a hermetic manifest without owned paths")
    for key in _OWNED_PATH_ENV:
        value = owned_paths.get(key)
        if not isinstance(value, str) or environment.get(key) != value:
            raise RuntimeError(f"cannot bind mismatched hermetic owned path: {key}")
        path = Path(value).expanduser().resolve(strict=False)
        if path != root and root not in path.parents:
            raise RuntimeError(f"cannot bind escaping hermetic owned path: {key}")
    binding = (root, manifest, environment_hash)
    if _PROCESS_RUN_BINDING is not None and _PROCESS_RUN_BINDING != binding:
        raise RuntimeError("hermetic test process is already bound to a different run")
    _PROCESS_RUN_BINDING = binding
    return root


def bound_process_run_identity() -> tuple[Path, Path, str] | None:
    """Return the pre-collection HRE identity, if the harness bound one."""

    return _PROCESS_RUN_BINDING


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_signature(path: Path) -> dict[str, object]:
    """Return a stable, non-mutating signature for a filesystem target."""

    try:
        stat = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if path.is_symlink():
        return {"exists": True, "kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        return {
            "exists": True,
            "kind": "file",
            "size": stat.st_size,
            "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "sha256": _file_digest(path),
        }
    if path.is_dir():
        entries: list[tuple[str, str, int, str]] = []
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                entries.append((str(child.relative_to(path)), "symlink", 0, os.readlink(child)))
            elif child.is_file():
                child_stat = child.stat()
                entries.append(
                    (
                        str(child.relative_to(path)),
                        "file",
                        child_stat.st_size,
                        _file_digest(child),
                    )
                )
            elif child.is_dir():
                entries.append((str(child.relative_to(path)), "dir", 0, ""))
        return {
            "exists": True,
            "kind": "directory",
            "tree_sha256": _sha256_text(_canonical_json(entries)),
            "entry_count": len(entries),
        }
    return {"exists": True, "kind": "other", "mode": stat.st_mode}


def discover_formal_state_targets(environment: Mapping[str, str]) -> tuple[Path, ...]:
    """Return the small, high-risk set validation historically mutated."""

    home = Path(environment.get("HOME") or Path.home()).expanduser().resolve(strict=False)
    mnemos = (
        Path(environment.get("MNEMOS_DIR") or home / ".mnemos").expanduser().resolve(strict=False)
    )
    database = (
        Path(environment.get("MNEMOS_DATABASE_DIR") or mnemos).expanduser().resolve(strict=False)
    )
    obsidian = environment.get("MNEMOS_OBSIDIAN_CONFIG_PATH")
    targets = {
        mnemos / "configs" / "main.json",
        database / "distillation_state.db",
        mnemos / "benchmarks" / "golden" / "latest",
    }
    if obsidian:
        targets.add(Path(obsidian).expanduser().resolve(strict=False))
    return tuple(sorted(targets))


@dataclass(frozen=True)
class HermeticRunEnvironment:
    root: Path
    profile: str
    environment: Mapping[str, str]
    environment_hash: str
    manifest_path: Path
    formal_targets: tuple[Path, ...]
    _formal_before: Mapping[str, Mapping[str, object]] = field(repr=False)

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        profile: str = "isolated",
        base_environment: Mapping[str, str] | None = None,
        formal_targets: tuple[Path, ...] | None = None,
        inherit_credentials: bool = False,
    ) -> "HermeticRunEnvironment":
        if profile not in PROFILES:
            raise ValueError(f"unknown hermetic run profile: {profile}")
        base = environment_snapshot() if base_environment is None else dict(base_environment)
        root = root.expanduser().resolve(strict=False)
        if root.exists():
            if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
                raise FileExistsError(f"hermetic run root is not empty: {root}")
        else:
            root.mkdir(parents=True, mode=0o700)

        paths = {
            "home": root / "home",
            "mnemos": root / "home" / ".mnemos",
            "database": root / "home" / ".mnemos",
            "wiki": root / "wiki",
            "raw": root / "raw",
            "config": root / "config",
            "cache": root / "cache",
            "data": root / "data",
            "tmp": root / "tmp",
            "pycache": root / "pycache",
            "artifacts": root / "artifacts",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        obsidian_config = paths["config"] / "obsidian.json"
        atomic_write_text(  # trusted-scan: config owner=ops target=hermetic_obsidian_config expires=never sandbox-only
            obsidian_config, '{"vaults": {}}\n'
        )

        inherited = {
            key: value
            for key, value in base.items()
            if key in _INHERITED_KEYS or (inherit_credentials and key in _CREDENTIAL_KEYS)
        }
        python_paths = [
            *str(base.get("PYTHONPATH", "")).split(os.pathsep),
            *(entry for entry in sys.path if entry),
        ]
        inherited["PYTHONPATH"] = os.pathsep.join(
            dict.fromkeys(
                str(Path(entry).expanduser().resolve(strict=False))
                for entry in python_paths
                if entry and Path(entry).expanduser().exists()
            )
        )
        owned = {
            "HOME": str(paths["home"]),
            "USERPROFILE": str(paths["home"]),
            "MNEMOS_DIR": str(paths["mnemos"]),
            "MNEMOS_DATABASE_DIR": str(paths["database"]),
            "MNEMOS_WIKI_DIR": str(paths["wiki"]),
            "MNEMOS_OBSIDIAN_CONFIG_PATH": str(obsidian_config),
            "XDG_CONFIG_HOME": str(paths["config"]),
            "XDG_CACHE_HOME": str(paths["cache"]),
            "XDG_DATA_HOME": str(paths["data"]),
            "TMPDIR": str(paths["tmp"]),
            "TEMP": str(paths["tmp"]),
            "TMP": str(paths["tmp"]),
            "PYTHONPYCACHEPREFIX": str(paths["pycache"]),
            "MNEMOS_RUN_ROOT": str(root),
            "MNEMOS_RUN_PROFILE": profile,
            "MNEMOS_RUN_ARTIFACTS_DIR": str(paths["artifacts"]),
            "MNEMOS_RUN_DEFAULT_MNEMOS_DIR": str(paths["mnemos"]),
            "MNEMOS_RUN_DEFAULT_DATABASE_DIR": str(paths["database"]),
            "MNEMOS_RUN_DEFAULT_WIKI_DIR": str(paths["wiki"]),
        }
        manifest_path = paths["artifacts"] / "environment-manifest.json"
        inherited.update(owned)
        inherited["MNEMOS_RUN_ENVIRONMENT_MANIFEST"] = str(manifest_path)
        environment_hash = _sha256_text(_canonical_json(inherited))
        inherited["MNEMOS_RUN_ENVIRONMENT_HASH"] = environment_hash

        targets = (
            formal_targets if formal_targets is not None else discover_formal_state_targets(base)
        )
        before = {str(path): path_signature(path) for path in targets}
        run = cls(
            root=root,
            profile=profile,
            environment=MappingProxyType(dict(inherited)),
            environment_hash=environment_hash,
            manifest_path=manifest_path,
            formal_targets=tuple(targets),
            _formal_before=MappingProxyType(before),
        )
        run._write_manifest(formal_state_diff=[])
        run.assert_owned_paths()
        return run

    def assert_owned_paths(self) -> None:
        for key in _OWNED_PATH_ENV:
            value = self.environment.get(key)
            if not value:
                raise ValueError(f"hermetic environment missing owned path: {key}")
            path = Path(value).expanduser().resolve(strict=False)
            if path != self.root and self.root not in path.parents:
                raise ValueError(f"hermetic path escapes run root: {key}={path}")

    def formal_state_diff(self) -> list[str]:
        after = {str(path): path_signature(path) for path in self.formal_targets}
        return sorted(
            path
            for path in set(self._formal_before) | set(after)
            if self._formal_before.get(path) != after.get(path)
        )

    def finalize(self) -> list[str]:
        diff = self.formal_state_diff()
        self._write_manifest(formal_state_diff=diff)
        return diff

    def report(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile": self.profile,
            "sandbox_root": str(self.root),
            "environment_hash": self.environment_hash,
            "manifest_path": str(self.manifest_path),
            "outside_write_count": len(self.formal_state_diff()),
            "formal_state_diff": self.formal_state_diff(),
        }

    def _write_manifest(self, *, formal_state_diff: list[str]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "profile": self.profile,
            "sandbox_root": str(self.root),
            "environment_hash": self.environment_hash,
            "manifest_path": str(self.manifest_path),
            "owned_paths": {key: self.environment[key] for key in _OWNED_PATH_ENV},
            "inherited_environment_keys": sorted(
                key
                for key in self.environment
                if key not in _OWNED_PATH_ENV
                and key
                not in {
                    "MNEMOS_RUN_ROOT",
                    "MNEMOS_RUN_PROFILE",
                    "MNEMOS_RUN_ENVIRONMENT_MANIFEST",
                    "MNEMOS_RUN_ENVIRONMENT_HASH",
                }
            ),
            "formal_state_targets": [str(path) for path in self.formal_targets],
            "outside_write_count": len(formal_state_diff),
            "formal_state_diff": formal_state_diff,
        }
        payload["integrity"] = {
            "scheme": MANIFEST_INTEGRITY_SCHEME,
            "digest": _manifest_integrity(payload),
        }
        atomic_write_text(  # trusted-scan: artifact owner=ops target=run_environment_manifest expires=never
            self.manifest_path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
