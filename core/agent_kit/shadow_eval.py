"""Golden shadow evaluation for one local CLI AgentBackend."""

from __future__ import annotations

import json
import shlex
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.agent_kit.agent_backend import AgentBackendConfig, CLIAgentBackend
from core.agent_kit.authorization import AgentAuthorizationStore
from core.agent_kit.protocol import TARGET_AGENT_NAMES, normalize_agent_name
from core.config import get_config
from core.trust.models import utc_now_iso


SHADOW_EVAL_SCHEMA_VERSION = "mnemos.agent_shadow_eval.v1"
SHADOW_CONFIG_SCHEMA_VERSION = "mnemos.agent_shadow_config.v1"
REQUIRED_FIELDS = ("summary", "keywords", "risk_level")

DEFAULT_GOLDEN_SAMPLES: tuple[dict[str, Any], ...] = (
    {
        "id": "decision-note",
        "task_type": "summary",
        "content": "User decided to keep trusted push enforce mode gated by human approval.",
    },
    {
        "id": "incident-note",
        "task_type": "summary",
        "content": "A local agent timed out during evaluation and Mnemos fell back safely.",
    },
    {
        "id": "preference-note",
        "task_type": "summary",
        "content": "The user prefers action-first repair loops with tests and local commits.",
    },
)


@dataclass(frozen=True)
class AgentShadowConfig:
    """Persisted single-agent shadow configuration."""

    enabled: bool = False
    agent: str = ""
    command: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    directory: str = "*"
    allowed_dirs: tuple[Path, ...] = ()
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["command"] = list(self.command)
        data["allowed_dirs"] = [str(path) for path in self.allowed_dirs]
        return data


class AgentShadowConfigStore:
    """SQLite store for the one allowed AgentBackend shadow candidate."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or get_config().database_dir / "agent_authorization.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_shadow_config (
                    id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    agent TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    timeout_seconds REAL NOT NULL,
                    directory TEXT NOT NULL,
                    allowed_dirs_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self) -> AgentShadowConfig:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_shadow_config WHERE id = 'active'"
            ).fetchone()
        if row is None:
            return AgentShadowConfig()
        return AgentShadowConfig(
            enabled=bool(row["enabled"]),
            agent=str(row["agent"]),
            command=tuple(json.loads(row["command_json"])),
            timeout_seconds=float(row["timeout_seconds"]),
            directory=str(row["directory"]),
            allowed_dirs=tuple(Path(p) for p in json.loads(row["allowed_dirs_json"])),
            updated_at=str(row["updated_at"]),
        )

    def enable(
        self,
        *,
        agent: str,
        command: Sequence[str],
        timeout_seconds: float = 30.0,
        directory: str = "*",
        allowed_dirs: Sequence[Path] = (),
    ) -> AgentShadowConfig:
        normalized = normalize_agent_name(agent)
        if normalized not in TARGET_AGENT_NAMES:
            raise ValueError(f"unsupported agent: {agent}")
        if not command:
            raise ValueError("command is required")
        now = utc_now_iso()
        allowed = [str(Path(path).expanduser().resolve()) for path in allowed_dirs]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_shadow_config (
                    id, schema_version, enabled, agent, command_json,
                    timeout_seconds, directory, allowed_dirs_json, updated_at
                ) VALUES ('active', ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SHADOW_CONFIG_SCHEMA_VERSION,
                    normalized,
                    json.dumps(list(command), ensure_ascii=False),
                    float(timeout_seconds),
                    directory or "*",
                    json.dumps(allowed, ensure_ascii=False),
                    now,
                ),
            )
        AgentAuthorizationStore(self.db_path).set_state(
            normalized,
            "shadow_enabled",
            directory=directory or "*",
            capability="content_analysis",
            purpose="distillation",
        )
        return self.get()

    def disable(self) -> AgentShadowConfig:
        current = self.get()
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_shadow_config (
                    id, schema_version, enabled, agent, command_json,
                    timeout_seconds, directory, allowed_dirs_json, updated_at
                ) VALUES ('active', ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SHADOW_CONFIG_SCHEMA_VERSION,
                    current.agent,
                    json.dumps(list(current.command), ensure_ascii=False),
                    float(current.timeout_seconds),
                    current.directory,
                    json.dumps([str(path) for path in current.allowed_dirs], ensure_ascii=False),
                    now,
                ),
            )
        if current.agent:
            AgentAuthorizationStore(self.db_path).set_state(
                current.agent,
                "revoked",
                directory=current.directory,
                capability="content_analysis",
                purpose="distillation",
            )
        return self.get()


class DeterministicBaselineBackend:
    """Repeatable baseline used by AgentBackend golden shadow eval."""

    def call(
        self,
        prompt: str,
        expect_json: bool = True,  # noqa: U100
        max_retries: int | None = None,  # noqa: U100
        response_max_tokens: int | None = None,  # noqa: U100
        response_retry_max_tokens: int | None = None,  # noqa: U100
    ) -> dict[str, Any]:
        words = [word.strip(".,:;!?()[]{}").lower() for word in prompt.split()]
        words = [word for word in words if word]
        keywords = sorted(set(words[:12]))[:5]
        return {
            "summary": " ".join(prompt.split()[:18]),
            "keywords": keywords,
            "risk_level": "low",
        }


def command_from_string(command: str) -> list[str]:
    parsed = shlex.split(command)
    if not parsed:
        raise ValueError("command is required")
    return parsed


def run_agent_shadow_eval(
    *,
    config_store: AgentShadowConfigStore | None = None,
    samples: Sequence[Mapping[str, Any]] = DEFAULT_GOLDEN_SAMPLES,
    confirm_send_content: bool = False,
    output_dir: Path | None = None,
    baseline_backend: DeterministicBaselineBackend | None = None,
) -> dict[str, Any]:
    store = config_store or AgentShadowConfigStore()
    shadow_config = store.get()
    if not shadow_config.enabled:
        return _disabled_result(shadow_config, "shadow config is disabled")
    if not confirm_send_content:
        return _disabled_result(shadow_config, "explicit content send confirmation required")

    backend = CLIAgentBackend(
        AgentBackendConfig(
            agent=shadow_config.agent,
            command=shadow_config.command,
            timeout_seconds=shadow_config.timeout_seconds,
            directory=shadow_config.directory,
            allowed_dirs=shadow_config.allowed_dirs,
        ),
        authorization_store=AgentAuthorizationStore(store.db_path),
    )
    baseline = baseline_backend or DeterministicBaselineBackend()

    cases: list[dict[str, Any]] = []
    for sample in samples:
        prompt = str(sample["content"])
        baseline_data = baseline.call(prompt, expect_json=True)
        shadow = backend.run(
            prompt,
            expect_json=True,
            task_type=str(sample.get("task_type") or "summary"),
            source_label=f"golden_eval:{sample['id']}",
        )
        baseline_completeness = _field_completeness(baseline_data)
        shadow_completeness = _field_completeness(shadow.data) if shadow.ok else 0.0
        cases.append(
            {
                "sample_id": sample["id"],
                "baseline_status": "ok",
                "shadow_status": shadow.status,
                "shadow_ok": shadow.ok,
                "schema_valid": shadow.ok and isinstance(shadow.data, dict),
                "baseline_field_completeness": baseline_completeness,
                "shadow_field_completeness": shadow_completeness,
                "field_completeness_delta": round(
                    baseline_completeness - shadow_completeness,
                    6,
                ),
                "fallback_to_baseline": not shadow.ok,
                "shadow_exit_code": shadow.exit_code,
                "shadow_elapsed_ms": round(shadow.elapsed_ms, 3),
                "shadow_failure_reason": shadow.failure_reason,
                "shadow_stderr_summary": shadow.stderr_summary,
                "shadow_stdout_hash": shadow.stdout_hash,
            }
        )

    result = _summarize(shadow_config, cases)
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "agent_shadow_eval.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result


def _disabled_result(config: AgentShadowConfig, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SHADOW_EVAL_SCHEMA_VERSION,
        "ok": False,
        "agent": config.agent,
        "shadow_enabled": config.enabled,
        "status": "disabled",
        "reason": reason,
        "metrics": {
            "sample_count": 0,
            "schema_success_rate": 0.0,
            "schema_failure_rate": 1.0,
            "fallback_rate": 1.0,
            "field_completeness_rate": 0.0,
            "quality_delta": 1.0,
            "shadow_write_count": 0,
        },
        "thresholds": _thresholds(),
        "cases": [],
    }


def _summarize(config: AgentShadowConfig, cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sample_count = len(cases)
    ok_count = sum(1 for case in cases if case["shadow_ok"])
    fallback_count = sum(1 for case in cases if case["fallback_to_baseline"])
    completeness = (
        sum(float(case["shadow_field_completeness"]) for case in cases) / sample_count
        if sample_count
        else 0.0
    )
    baseline_completeness = (
        sum(float(case["baseline_field_completeness"]) for case in cases) / sample_count
        if sample_count
        else 0.0
    )
    schema_success_rate = ok_count / sample_count if sample_count else 0.0
    fallback_rate = fallback_count / sample_count if sample_count else 1.0
    quality_delta = max(0.0, baseline_completeness - completeness)
    thresholds = _thresholds()
    ok = (
        sample_count > 0
        and (1.0 - schema_success_rate) <= thresholds["max_schema_failure_rate"]
        and fallback_rate <= thresholds["max_fallback_rate"]
        and quality_delta <= thresholds["max_quality_delta"]
    )
    return {
        "schema_version": SHADOW_EVAL_SCHEMA_VERSION,
        "ok": ok,
        "agent": config.agent,
        "shadow_enabled": config.enabled,
        "status": "ok" if ok else "threshold_failed",
        "metrics": {
            "sample_count": sample_count,
            "schema_success_rate": round(schema_success_rate, 6),
            "schema_failure_rate": round(1.0 - schema_success_rate, 6),
            "fallback_rate": round(fallback_rate, 6),
            "field_completeness_rate": round(completeness, 6),
            "quality_delta": round(quality_delta, 6),
            "shadow_write_count": 0,
        },
        "thresholds": thresholds,
        "cases": list(cases),
    }


def _field_completeness(payload: Mapping[str, Any]) -> float:
    if not REQUIRED_FIELDS:
        return 1.0
    present = 0
    for field_name in REQUIRED_FIELDS:
        value = payload.get(field_name)
        if value not in (None, "", [], {}):
            present += 1
    return present / len(REQUIRED_FIELDS)


def _thresholds() -> dict[str, float]:
    return {
        "max_schema_failure_rate": 0.05,
        "max_quality_delta": 0.10,
        "max_fallback_rate": 0.20,
    }
