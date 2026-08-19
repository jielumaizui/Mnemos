"""Recoverable persistence and ACL denominator scanning for Wiki ANN indexes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Dict, List, Tuple, TYPE_CHECKING
import uuid

from core.access_policy import validate_acl_envelope
from core.frontmatter import read_strict_frontmatter_document
from core.utils import read_bytes_value, read_text_value


class EmbeddingIndexPersistenceMixin:
    """Durable generation commits and fail-closed Wiki page eligibility."""

    if TYPE_CHECKING:
        wiki_base: Path
        index_dir: Path
        _index_path: Path
        _meta_path: Path
        _generation_manifest_path: Path
        _generation_recovery_required: bool
        _meta: Dict[str, Dict]
        _index: Any
        _id_to_chunk: Dict[int, Tuple[str, int]]
        _memory_fallback: List[Tuple[str, int, List[float]]]
        DIM: int

    def _load_meta(self) -> None:
        if self._generation_manifest_path.is_file():
            self._generation_recovery_required = True
            self._meta = {}
            return
        if self._meta_path.exists():
            try:
                self._meta = json.loads(read_text_value(self._meta_path))
            except (json.JSONDecodeError, ValueError):
                self._meta = {}

    def _save_meta(self) -> None:
        """Persist metadata only from an explicit index-build flow."""

        self.index_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.index_dir / f".{self._meta_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            self._write_json_artifact(temporary, self._meta)
            os.replace(temporary, self._meta_path)
            self._fsync_index_directory()
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _artifact_digest(path: Path) -> str:
        return "sha256:" + hashlib.sha256(read_bytes_value(path)).hexdigest()

    @staticmethod
    def _write_json_artifact(path: Path, payload: Any) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _fsync_index_directory(self) -> None:
        descriptor = os.open(self.index_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_generation_manifest(self, payload: Dict[str, Any]) -> None:
        temporary = self.index_dir / (
            f".{self._generation_manifest_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            self._write_json_artifact(temporary, payload)
            os.replace(temporary, self._generation_manifest_path)
            self._fsync_index_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _recover_persisted_generation(self) -> bool:
        """Restore the previous complete index generation after an interrupted commit."""

        manifest_path = self._generation_manifest_path
        if not manifest_path.is_file():
            self._generation_recovery_required = False
            return False
        try:
            manifest = json.loads(read_text_value(manifest_path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Wiki index generation recovery manifest is invalid") from exc
        status = str(manifest.get("status") or "")
        if manifest.get("schema_version") != "mnemos.wiki_index_generation.v1" or status not in {
            "prepared",
            "committed",
        }:
            raise RuntimeError("Wiki index generation recovery manifest is unsupported")
        records = manifest.get("artifacts")
        if not isinstance(records, list) or len(records) != 2:
            raise RuntimeError("Wiki index generation recovery artifact set is invalid")
        expected_targets = {self._index_path.name, self._meta_path.name}
        seen_targets: set[str] = set()
        prepared: list[tuple[Path, Path | None, Path | None, bool]] = []
        for raw in records:
            if not isinstance(raw, dict):
                raise RuntimeError("Wiki index generation recovery record is invalid")
            target_name = str(raw.get("target") or "")
            if target_name not in expected_targets or target_name in seen_targets:
                raise RuntimeError("Wiki index generation recovery target is invalid")
            seen_targets.add(target_name)
            target = self.index_dir / target_name
            existed = bool(raw.get("existed"))
            backup_name = str(raw.get("backup") or "")
            backup = self.index_dir / backup_name if backup_name else None
            if backup_name and Path(backup_name).name != backup_name:
                raise RuntimeError("Wiki index generation recovery backup path is invalid")
            if existed and status == "prepared":
                if (
                    backup is None
                    or backup.parent != self.index_dir
                    or not backup.is_file()
                    or self._artifact_digest(backup) != str(raw.get("backup_sha256") or "")
                ):
                    raise RuntimeError("Wiki index generation recovery backup is invalid")
            stage_name = str(raw.get("stage") or "")
            stage = self.index_dir / stage_name if stage_name else None
            if stage_name and Path(stage_name).name != stage_name:
                raise RuntimeError("Wiki index generation recovery stage is invalid")
            prepared.append((target, backup, stage, existed))
        if seen_targets != expected_targets:
            raise RuntimeError("Wiki index generation recovery targets are incomplete")

        if status == "committed":
            for raw, (target, _backup, _stage, _existed) in zip(records, prepared):
                expected_sha256 = str(raw.get("committed_sha256") or "")
                actual_sha256 = self._artifact_digest(target) if target.is_file() else ""
                if actual_sha256 != expected_sha256:
                    raise RuntimeError("Wiki index committed generation verification failed")
            for _target, backup, stage, _existed in prepared:
                if backup is not None:
                    backup.unlink(missing_ok=True)
                if stage is not None:
                    stage.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            self._fsync_index_directory()
            self._generation_recovery_required = False
            return True

        restore_stages: list[Path] = []
        try:
            for target, backup, _stage, existed in prepared:
                if existed:
                    assert backup is not None
                    restore = self.index_dir / f".{target.name}.{uuid.uuid4().hex}.restore"
                    shutil.copy2(backup, restore)
                    restore_stages.append(restore)
                    os.replace(restore, target)
                else:
                    target.unlink(missing_ok=True)
            self._fsync_index_directory()
        except BaseException as exc:
            raise RuntimeError("Wiki index generation rollback failed") from exc
        finally:
            for restore in restore_stages:
                restore.unlink(missing_ok=True)

        for _target, backup, stage, _existed in prepared:
            if backup is not None:
                backup.unlink(missing_ok=True)
            if stage is not None:
                stage.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        self._fsync_index_directory()
        self._generation_recovery_required = False
        return True

    def _persist_index_generation(self, backend: str) -> None:
        """Commit metadata and ANN bytes as one recoverable durable generation."""

        if backend not in {"hnswlib", "memory"}:
            raise ValueError("unsupported Wiki index backend")
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if self._generation_manifest_path.is_file():
            self._recover_persisted_generation()
        transaction_id = uuid.uuid4().hex
        meta_stage = self.index_dir / f".{self._meta_path.name}.{transaction_id}.tmp"
        index_stage = self.index_dir / f".{self._index_path.name}.{transaction_id}.tmp"
        records: list[Dict[str, Any]] = []
        try:
            self._write_json_artifact(meta_stage, self._meta)
            if backend == "hnswlib":
                if self._index is None:
                    raise RuntimeError("HNSW generation lacks an in-memory index")
                self._index.save_index(str(index_stage))
                with index_stage.open("rb") as handle:
                    os.fsync(handle.fileno())

            for target, stage in (
                (self._index_path, index_stage if backend == "hnswlib" else None),
                (self._meta_path, meta_stage),
            ):
                existed = target.is_file()
                backup = self.index_dir / f".{target.name}.{transaction_id}.bak"
                if existed:
                    shutil.copy2(target, backup)
                    with backup.open("rb") as handle:
                        os.fsync(handle.fileno())
                records.append(
                    {
                        "target": target.name,
                        "stage": stage.name if stage is not None else "",
                        "backup": backup.name if existed else "",
                        "backup_sha256": (self._artifact_digest(backup) if existed else ""),
                        "existed": existed,
                    }
                )
            manifest = {
                "schema_version": "mnemos.wiki_index_generation.v1",
                "status": "prepared",
                "backend": backend,
                "transaction_id": transaction_id,
                "artifacts": records,
            }
            self._write_generation_manifest(manifest)
            if backend == "hnswlib":
                os.replace(index_stage, self._index_path)
            else:
                self._index_path.unlink(missing_ok=True)
            os.replace(meta_stage, self._meta_path)
            decoded = json.loads(read_text_value(self._meta_path))
            if decoded != self._meta:
                raise RuntimeError("Wiki index metadata verification failed")
            if backend == "hnswlib" and not self._index_path.is_file():
                raise RuntimeError("Wiki HNSW generation verification failed")
            self._fsync_index_directory()
            for record in records:
                target = self.index_dir / str(record["target"])
                record["committed_sha256"] = (
                    self._artifact_digest(target) if target.is_file() else ""
                )
            manifest["status"] = "committed"
            self._write_generation_manifest(manifest)
        except BaseException:
            if self._generation_manifest_path.is_file():
                self._recover_persisted_generation()
            else:
                for path in (index_stage, meta_stage):
                    path.unlink(missing_ok=True)
                for target in (self._index_path, self._meta_path):
                    (self.index_dir / f".{target.name}.{transaction_id}.bak").unlink(
                        missing_ok=True
                    )
            raise
        for record in records:
            backup_name = str(record["backup"] or "")
            if backup_name:
                (self.index_dir / backup_name).unlink(missing_ok=True)
        self._generation_manifest_path.unlink(missing_ok=True)
        self._fsync_index_directory()
        self._generation_recovery_required = False

    def _scan_wiki_pages(self) -> List[Path]:
        """Scan every non-hidden Markdown page under the configured Wiki root."""

        if not self.wiki_base.exists():
            return []
        pages = []
        for page in self.wiki_base.rglob("*.md"):
            try:
                rel_parts = page.relative_to(self.wiki_base).parts
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel_parts):
                continue
            pages.append(page)
        return sorted(pages)

    def _index_eligibility(self, page: Path) -> str:
        """Return an exclusion reason, or empty text for an ANN-eligible page."""

        try:
            relative = page.relative_to(self.wiki_base)
        except ValueError:
            return "outside_wiki_root"
        if relative.parts:
            from core.cognitive.sources import SourceReader

            if relative.parts[0] in SourceReader.SYSTEM_DIRS:
                return "system_generated_projection"
        try:
            with page.open("r", encoding="utf-8", errors="strict") as handle:
                if handle.read(4) not in {"---\n", "---\r"}:
                    return "acl_metadata_missing"
        except (OSError, UnicodeError):
            return "acl_parse_error"
        try:
            frontmatter, _body, _content = read_strict_frontmatter_document(
                page,
                errors="strict",
            )
        except (OSError, UnicodeError, ValueError):
            return "acl_parse_error"
        decision = validate_acl_envelope({**frontmatter, "page_path": relative.as_posix()})
        return "" if decision.allowed else decision.reason

    def _scan_indexable_wiki_pages(self) -> Tuple[List[Path], Dict[str, List[str]]]:
        """Resolve the fail-closed ANN denominator without reading denied bodies."""

        included: List[Path] = []
        excluded: Dict[str, List[str]] = {}
        for page in self._scan_wiki_pages():
            reason = self._index_eligibility(page)
            if not reason:
                included.append(page)
                continue
            excluded.setdefault(reason, []).append(page.relative_to(self.wiki_base).as_posix())
        return included, excluded

    def _rebuild_id_to_chunk_from_meta(self) -> None:
        """Rebuild id -> (relative path, chunk index) from durable metadata."""

        self._id_to_chunk = {}
        for rel_path, meta in self._meta.items():
            for chunk_info in meta.get("chunks", []):
                if "id" not in chunk_info:
                    continue
                chunk_id = chunk_info["id"]
                chunk_idx = chunk_info.get("chunk_idx", 0)
                self._id_to_chunk[chunk_id] = (rel_path, chunk_idx)

    def _restore_memory_fallback_from_meta(self) -> None:
        """Restore the durable memory backend from JSON metadata after restart."""

        restored: List[Tuple[str, int, List[float]]] = []
        for rel_path, meta in self._meta.items():
            for chunk in meta.get("chunks", []):
                embedding = chunk.get("embedding")
                if not isinstance(embedding, list) or len(embedding) != self.DIM:
                    continue
                try:
                    vector = [float(value) for value in embedding]
                except (TypeError, ValueError):
                    continue
                restored.append((rel_path, int(chunk.get("chunk_idx", 0)), vector))
        self._memory_fallback = restored
        self._rebuild_id_to_chunk_from_meta()


__all__ = ["EmbeddingIndexPersistenceMixin"]
