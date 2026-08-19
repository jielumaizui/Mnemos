# -*- coding: utf-8 -*-
"""
Vault Sync — 手动/兜底全量重建认知层 Markdown 投影

职责：
- 把 SQLite 中的认知层数据统一投影到配置中的主 Vault
- 覆盖 L2.4-KG / L3-Observations / L4-Reflections / L5-Feedback
- 投影完成后对 Vault 执行一次 git 快照（如未初始化则自动 init）

注意：
- 本模块只做“投影 + 快照”，不修改原始 SQLite 数据。
- 各层已有自己的增量投影路径；这里提供手动全量兜底入口。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from core.config import get_config

# Constants extracted from magic numbers
INIT_SECONDS = 30
ADD_SECONDS = 30
COMMIT_SECONDS = 30

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.wiki_derived_projection import DerivedProjectionLifecycle


_CANONICAL_SOURCE_DATABASES = (
    "knowledge_graph.db",
    "cognitive_graph.db",
    "observations.db",
    "reflections.db",
    "user_signals.db",
    "producer_consumer_ledger.db",
)


def _sqlite_artifact_fallback_hash(path: Path) -> bytes:
    """Hash main and WAL bytes when SQLite cannot expose a logical snapshot."""

    digest = hashlib.sha256()
    for suffix in ("", "-wal"):
        artifact = Path(f"{path}{suffix}")
        if not artifact.is_file():
            continue
        label = suffix or "-main"
        digest.update(label.encode("ascii"))
        digest.update(artifact.stat().st_size.to_bytes(8, "big"))
        with artifact.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.digest()


def _sqlite_logical_image(connection: sqlite3.Connection) -> bytes:
    """Serialize one read transaction or fail into the artifact fallback path."""

    serializer = getattr(connection, "serialize", None)
    if not callable(serializer):
        raise sqlite3.NotSupportedError("SQLite connection serialization is unavailable")
    image = serializer()
    if not isinstance(image, bytes):
        raise sqlite3.DatabaseError("SQLite connection returned a non-bytes image")
    return image


def _canonical_source_hashes(database_dir: Path) -> Dict[str, str]:
    """Hash each canonical database's current logical SQLite image."""

    hashes: Dict[str, str] = {}
    for name in _CANONICAL_SOURCE_DATABASES:
        path = database_dir / name
        if not path.is_file():
            hashes[name] = "missing"
            continue
        try:
            connection = sqlite3.connect(
                f"file:{path.resolve(strict=True)}?mode=ro",
                uri=True,
                timeout=30,
            )
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                logical_image = _sqlite_logical_image(connection)
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            # Preserve fail-closed delta detection even when a WAL snapshot
            # cannot be opened without touching production sidecars.
            logical_image = _sqlite_artifact_fallback_hash(path)
        hashes[name] = "sha256:" + hashlib.sha256(logical_image).hexdigest()
    return hashes


def _vault_git_commit(vault_dir: Path, message: str) -> Dict[str, Any]:
    """尝试对 vault 目录做一次 git 快照。"""
    result = {"committed": False, "output": ""}
    if not vault_dir.exists():
        return result
    try:
        git_dir = vault_dir / ".git"
        if not git_dir.exists():
            init = subprocess.run(
                ["git", "init", str(vault_dir)],
                capture_output=True,
                text=True,
                timeout=INIT_SECONDS,
            )
            result["output"] += init.stdout + init.stderr  # type: ignore[operator]

        add = subprocess.run(
            ["git", "-C", str(vault_dir), "add", "."],
            capture_output=True,
            text=True,
            timeout=ADD_SECONDS,
        )
        result["output"] += add.stdout + add.stderr  # type: ignore[operator]

        commit = subprocess.run(
            ["git", "-C", str(vault_dir), "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=COMMIT_SECONDS,
        )
        result["output"] += commit.stdout + commit.stderr  # type: ignore[operator]
        result["committed"] = commit.returncode == 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        result["output"] += str(exc)  # type: ignore[operator]
        logger.warning("[VaultSync] git 快照失败: %s", exc)
    return result


def sync_kg_projection(
    vault_dir: Path,
    *,
    config: Any | None = None,
    lifecycle: "DerivedProjectionLifecycle | None" = None,
) -> Dict[str, Any]:
    """全量同步 L2.4 KG 投影。"""
    result = {"status": "ok", "entities": 0, "relations": 0, "error": ""}
    try:
        from core.kia.kg_exporter import KGExporter
        from core.kia.knowledge_graph import KnowledgeGraph

        cfg = config or get_config()
        db_path = Path(cfg.database_dir).expanduser() / "knowledge_graph.db"
        if not db_path.is_file():
            raise FileNotFoundError(db_path)
        graph = KnowledgeGraph(
            db_path=str(db_path),
            wiki_base=str(vault_dir),
            initialize=False,
            read_only=True,
            config=cfg,
        )
        try:
            exporter = KGExporter(
                str(vault_dir),
                kg=graph,
                lifecycle=lifecycle,
                emit_runtime_consumption=False,
            )
            stats = exporter.export_to_vault()
        finally:
            graph.close()
        result["entities"] = stats.get("entities", 0)
        result["relations"] = stats.get("relations", 0)
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        logger.warning("[VaultSync] KG 投影失败: %s", exc)
    return result


def sync_observation_projection(
    vault_dir: Path,
    *,
    config: Any | None = None,
    lifecycle: "DerivedProjectionLifecycle | None" = None,
) -> Dict[str, Any]:
    """全量同步 L3 Observations 投影。"""
    result = {"status": "ok", "observations": 0, "dimensions": 0, "error": ""}
    try:
        from core.cognitive.observation_projection import (
            rebuild_observation_projection,
        )
        cfg = config or get_config()
        database_dir = Path(cfg.database_dir).expanduser()
        replay = rebuild_observation_projection(
            wiki_dir=vault_dir,
            observation_db_path=database_dir / "observations.db",
            cognitive_state_db_path=database_dir / "producer_consumer_ledger.db",
            lifecycle=lifecycle,
        )
        result["observations"] = replay.observation_count
        result["dimensions"] = replay.dimension_count
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        logger.warning("[VaultSync] Observation 投影失败: %s", exc)
    return result


def sync_reflection_projection(
    vault_dir: Path,
    *,
    config: Any | None = None,
    lifecycle: "DerivedProjectionLifecycle | None" = None,
) -> Dict[str, Any]:
    """全量同步 L4 Reflections 投影。"""
    result = {"status": "ok", "records": 0, "shifts": 0, "error": ""}
    try:
        from core.reflection.reflection_exporter import ReflectionExporter
        from core.reflection.reflection_store import ReflectionStore

        cfg = config or get_config()
        db_path = Path(cfg.database_dir).expanduser() / "reflections.db"
        store = ReflectionStore(
            str(db_path),
            initialize=False,
            read_only=True,
        )
        exporter = ReflectionExporter(str(vault_dir), lifecycle=lifecycle)
        stats = exporter.export_all(store)
        result["records"] = stats.get("records", 0)
        result["shifts"] = stats.get("shifts", 0)
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        logger.warning("[VaultSync] Reflection 投影失败: %s", exc)
    return result


def sync_persona_projection(
    vault_dir: Path,
    *,
    config: Any | None = None,
    lifecycle: "DerivedProjectionLifecycle | None" = None,
) -> Dict[str, Any]:
    """全量同步 L5 Feedback / Persona 投影。"""
    result: Dict[str, Any] = {"status": "ok", "version": None, "error": ""}
    try:
        from core.persona.delphi import PersonaStore

        cfg = config or get_config()
        db_path = Path(cfg.database_dir).expanduser() / "user_signals.db"
        versions = PersonaStore.load_canonical_persona_versions_read_only(db_path)
        store = PersonaStore.for_projection_replay(
            wiki_dir=vault_dir,
            canonical_db_path=db_path,
            projection_lifecycle=lifecycle,
        )
        store.project_all_personas(versions)
        if versions:
            result["version"] = versions[0][0].version
        else:
            result["status"] = "skipped"
            result["error"] = "无可用画像"
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        logger.warning("[VaultSync] Persona 投影失败: %s", exc)
    return result


def sync_all_projections(
    vault_dir: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
    commit: bool = True,
    *,
    config: Any | None = None,
    lifecycle: "DerivedProjectionLifecycle | None" = None,
) -> Dict[str, Any]:
    """
    重建主认知 Vault 的全部 Markdown 投影。

    Returns:
        dict: 各层结果与 git 快照状态
    """
    cfg = config or get_config()
    vault_dir = vault_dir or cfg.vault_dir("mnemos")
    raw_dir = raw_dir or cfg.vault_dir("raw")
    database_dir = Path(cfg.database_dir).expanduser().resolve(strict=False)
    before_hashes = _canonical_source_hashes(database_dir)

    vault_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    if lifecycle is None:
        from core.wiki_derived_projection import DerivedProjectionLifecycle
        from core.wiki_projection_lifecycle import (
            WikiProjectionLedger,
            resolve_wiki_projection_db_path,
        )

        lifecycle = DerivedProjectionLifecycle(
            vault_dir,
            ledger=WikiProjectionLedger(resolve_wiki_projection_db_path(cfg)),
        )

    summary: Dict[str, Any] = {
        "vault_dir": str(vault_dir),
        "raw_dir": str(raw_dir),
        "kg": sync_kg_projection(vault_dir, config=cfg, lifecycle=lifecycle),
        "observation": sync_observation_projection(
            vault_dir,
            config=cfg,
            lifecycle=lifecycle,
        ),
        "reflection": sync_reflection_projection(
            vault_dir,
            config=cfg,
            lifecycle=lifecycle,
        ),
        "persona": sync_persona_projection(
            vault_dir,
            config=cfg,
            lifecycle=lifecycle,
        ),
    }

    after_hashes = _canonical_source_hashes(database_dir)
    canonical_delta = {
        name: {"before": before_hashes[name], "after": after_hashes[name]}
        for name in before_hashes
        if before_hashes[name] != after_hashes[name]
    }
    summary["canonical_source_hashes"] = after_hashes
    summary["canonical_delta"] = canonical_delta
    if canonical_delta:
        changed = ", ".join(sorted(canonical_delta))
        raise RuntimeError(f"vault sync mutated canonical source databases: {changed}")

    layer_statuses = {
        str(summary[layer].get("status", "error"))
        for layer in ("kg", "observation", "reflection", "persona")
    }
    summary["status"] = "ok" if layer_statuses <= {"ok", "skipped"} else "error"

    if commit and summary["status"] == "ok":
        ts = datetime.now().isoformat(timespec="seconds")
        summary["git"] = _vault_git_commit(
            vault_dir,
            f"vault-sync: rebuild projections @ {ts}",
        )
    elif commit:
        summary["git"] = {
            "committed": False,
            "output": "skipped: projection error",
        }
    else:
        summary["git"] = {"committed": False, "output": "skipped"}

    return summary
