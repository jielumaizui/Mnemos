"""Typed contracts for exact leaked demo-fixture reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.cognitive.state_contract import sha256_json


RECONCILIATION_SCHEMA_VERSION = "mnemos.demo_fixture_reconciliation.v1"
MIGRATION_ID = "database.demo_fixture_leak.v1"
FIXTURE_SESSION_ID = "demo-asyncio-gather"
FIXTURE_TITLE = "asyncio.gather 并发请求 TimeoutError 定位与兜底"
FIXTURE_QUOTE = "明白了，我这就去把现有代码改成 return_exceptions=True + 单独超时包装。"
FIXTURE_CLAIM_ID = "claim-asyncio-gather-timeout"
FIXTURE_CLAIM_TEXT = (
    "当前 asyncio.gather 并发请求将采用单任务超时包装，"
    "并配合 return_exceptions=True 避免一个失败拖垮整批。"
)
FIXTURE_BEHAVIOR_SUMMARY = "用户需要沉淀 asyncio 并发超时排查方法。"
FIXTURE_INTENT_REASON = "用户明确确认采用并发超时修复方案。"


@dataclass(frozen=True)
class DemoFixtureReconciliationPaths:
    """Exact stores and tracked fixture source participating in the review."""

    database_dir: Path
    repo_root: Path

    @property
    def state_path(self) -> Path:
        return self.database_dir / "producer_consumer_ledger.db"

    @property
    def raw_path(self) -> Path:
        return self.database_dir / "raw_events.db"

    @property
    def action_path(self) -> Path:
        return self.database_dir / "action_ledger.db"

    @property
    def migrations_path(self) -> Path:
        return self.database_dir / "migrations.db"

    @property
    def fixture_source_path(self) -> Path:
        return self.repo_root / "docs" / "demo" / "run_demo.py"


@dataclass(frozen=True)
class DemoFixtureCommandClosure:
    command_id: str
    consumer_id: str
    payload_hash: str

    def manifest(self) -> dict[str, str]:
        return {
            "command_id": self.command_id,
            "consumer_id": self.consumer_id,
            "payload_hash": self.payload_hash,
        }


@dataclass(frozen=True)
class DemoFixtureEpisodeRetirement:
    object_id: str
    revision_id: str
    payload_hash: str
    revision_row_hash: str
    event_id: str
    event_row_hash: str
    source_revision_ids: tuple[str, ...]
    commands: tuple[DemoFixtureCommandClosure, ...]

    def manifest(self) -> dict[str, Any]:
        return {
            "action": "retire_demo_cognition_episode",
            "object_id": self.object_id,
            "revision_id": self.revision_id,
            "payload_hash": self.payload_hash,
            "revision_row_hash": self.revision_row_hash,
            "event_id": self.event_id,
            "event_row_hash": self.event_row_hash,
            "source_revision_ids": list(self.source_revision_ids),
            "commands": [value.manifest() for value in self.commands],
        }


@dataclass(frozen=True)
class DemoFixtureActionSkip:
    action_id: str
    action_row_hash: str
    production_event_id: str
    runtime_event_row_hash: str
    consumer_id: str

    def manifest(self) -> dict[str, str]:
        return {
            "action": "skip_demo_quality_admission",
            "action_id": self.action_id,
            "action_row_hash": self.action_row_hash,
            "production_event_id": self.production_event_id,
            "runtime_event_row_hash": self.runtime_event_row_hash,
            "consumer_id": self.consumer_id,
        }


@dataclass(frozen=True)
class DemoFixtureReconciliationPlan:
    paths: DemoFixtureReconciliationPaths
    fixture_source_hash: str
    episodes: tuple[DemoFixtureEpisodeRetirement, ...]
    actions: tuple[DemoFixtureActionSkip, ...]
    blocked: tuple[Mapping[str, str], ...]
    object_manifest_hash: str
    inventory_hash: str

    @property
    def ok(self) -> bool:
        return not self.blocked

    @property
    def requires_apply(self) -> bool:
        return bool(self.episodes or self.actions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "ok": self.ok,
            "status": (
                "blocked"
                if not self.ok
                else "reconciliation_required"
                if self.requires_apply
                else "clean"
            ),
            "fixture_source_hash": self.fixture_source_hash,
            "inventory_hash": self.inventory_hash,
            "object_manifest_hash": self.object_manifest_hash,
            "counts": {
                "retire_demo_cognition_episode": len(self.episodes),
                "skip_demo_quality_admission": len(self.actions),
                "blocked": len(self.blocked),
            },
            "blocked": [dict(value) for value in self.blocked],
            "paths": {
                "database_dir": str(self.paths.database_dir),
                "fixture_source": str(self.paths.fixture_source_path),
            },
        }


def finalize_plan_hashes(
    *,
    fixture_source_hash: str,
    episode_manifests: Sequence[Mapping[str, Any]],
    action_manifests: Sequence[Mapping[str, Any]],
    blocked: Sequence[Mapping[str, str]],
) -> tuple[str, str]:
    object_manifest_hash = sha256_json(
        {
            "episodes": list(episode_manifests),
            "actions": list(action_manifests),
            "blocked": list(blocked),
        }
    )
    return object_manifest_hash, sha256_json(
        {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "fixture_source_hash": fixture_source_hash,
            "object_manifest_hash": object_manifest_hash,
        }
    )


__all__ = [
    "DemoFixtureActionSkip",
    "DemoFixtureCommandClosure",
    "DemoFixtureEpisodeRetirement",
    "DemoFixtureReconciliationPaths",
    "DemoFixtureReconciliationPlan",
    "FIXTURE_BEHAVIOR_SUMMARY",
    "FIXTURE_CLAIM_ID",
    "FIXTURE_CLAIM_TEXT",
    "FIXTURE_INTENT_REASON",
    "FIXTURE_QUOTE",
    "FIXTURE_SESSION_ID",
    "FIXTURE_TITLE",
    "MIGRATION_ID",
    "RECONCILIATION_SCHEMA_VERSION",
    "finalize_plan_hashes",
]
