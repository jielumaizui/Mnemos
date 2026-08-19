"""CLI AgentBackend for shadow-only local agent evaluation."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from core.agent_kit.authorization import AgentAuthorizationStore
from core.agent_kit.prompt_sanitizer import PromptSanitizer
from core.trust.models import sha256_text


REQUEST_SCHEMA_VERSION = "mnemos.agent_backend.request.v1"
RESULT_SCHEMA_VERSION = "mnemos.agent_backend.result.v1"
SHADOW_REQUIRED_STATE = "shadow_enabled"

_RUN_ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)
_MAX_STDERR_SUMMARY_CHARS = 500


@dataclass(frozen=True)
class AgentBackendConfig:
    """Configuration for one non-interactive CLI agent process."""

    agent: str
    command: Sequence[str]
    timeout_seconds: float = 30.0
    cwd: str = ""
    directory: str = "*"
    allowed_dirs: Sequence[Path] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.agent.strip():
            raise ValueError("agent is required")
        if not self.command:
            raise ValueError("command is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class AgentBackendResult:
    """Observable result from a shadow AgentBackend subprocess."""

    schema_version: str
    agent: str
    status: str
    ok: bool
    elapsed_ms: float
    exit_code: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    failure_reason: str = ""
    stderr_summary: str = ""
    stdout_hash: str = ""
    sanitizer_event_id: str = ""
    findings: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CLIAgentBackend:
    """Run a local CLI agent in shadow mode.

    The prompt is sent through stdin as structured JSON. It is never passed as a
    subprocess argument. The backend refuses to run unless the selected agent has
    explicit ``shadow_enabled`` authorization and the PromptSanitizer allows both
    prompt and subprocess argument surfaces.
    """

    def __init__(
        self,
        config: AgentBackendConfig,
        *,
        authorization_store: AgentAuthorizationStore | None = None,
        sanitizer: PromptSanitizer | None = None,
    ):
        self.config = config
        self._authorization_store = authorization_store or AgentAuthorizationStore()
        self._sanitizer = sanitizer or PromptSanitizer()

    def run(
        self,
        prompt: str,
        *,
        expect_json: bool = True,
        task_type: str = "summary",
        source_label: str = "agent_backend",
    ) -> AgentBackendResult:
        started = time.monotonic()
        auth = self._authorization_record()
        if auth is None or auth.state != SHADOW_REQUIRED_STATE:
            return self._result(
                status="unauthorized",
                ok=False,
                started=started,
                failure_reason="agent shadow is not enabled",
            )

        sanitize_args = list(self.config.command[1:])
        sanitized = self._sanitizer.sanitize(
            agent=self.config.agent,
            text=prompt,
            args=sanitize_args,
            source_label=source_label,
            allowed_dirs=self.config.allowed_dirs,
        )
        if not sanitized.allowed:
            return self._result(
                status="sanitizer_blocked",
                ok=False,
                started=started,
                failure_reason="prompt or subprocess args rejected by sanitizer",
                sanitizer_event_id=sanitized.audit_event_id,
                findings=[finding.to_dict() for finding in sanitized.findings],
            )

        request = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "agent": self.config.agent,
            "task_type": task_type,
            "expect_json": bool(expect_json),
            "prompt": sanitized.redacted_text,
        }
        try:
            proc = subprocess.Popen(
                list(self.config.command),
                cwd=self.config.cwd or None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except _RUN_ERRORS as exc:
            return self._result(
                status="spawn_failed",
                ok=False,
                started=started,
                failure_reason=type(exc).__name__,
            )

        try:
            stdout, stderr = proc.communicate(
                json.dumps(request, ensure_ascii=False),
                timeout=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            self._kill_process_tree(proc)
            stdout, stderr = proc.communicate()
            return self._result(
                status="timeout",
                ok=False,
                started=started,
                exit_code=proc.returncode,
                failure_reason="timeout",
                stderr_summary=self._stderr_summary(stderr),
                stdout_hash=sha256_text(stdout or ""),
                sanitizer_event_id=sanitized.audit_event_id,
            )

        stdout_text = stdout or ""
        stderr_summary = self._stderr_summary(stderr or "")
        if proc.returncode != 0:
            return self._result(
                status="exit_nonzero",
                ok=False,
                started=started,
                exit_code=proc.returncode,
                failure_reason=f"exit_code={proc.returncode}",
                stderr_summary=stderr_summary,
                stdout_hash=sha256_text(stdout_text),
                sanitizer_event_id=sanitized.audit_event_id,
            )
        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError:
            return self._result(
                status="schema_invalid",
                ok=False,
                started=started,
                exit_code=proc.returncode,
                failure_reason="stdout is not valid JSON",
                stderr_summary=stderr_summary,
                stdout_hash=sha256_text(stdout_text),
                sanitizer_event_id=sanitized.audit_event_id,
            )
        if not isinstance(payload, dict):
            return self._result(
                status="schema_invalid",
                ok=False,
                started=started,
                exit_code=proc.returncode,
                failure_reason="stdout JSON must be an object",
                stderr_summary=stderr_summary,
                stdout_hash=sha256_text(stdout_text),
                sanitizer_event_id=sanitized.audit_event_id,
            )
        return self._result(
            status="ok",
            ok=True,
            started=started,
            exit_code=proc.returncode,
            data=payload,
            stderr_summary=stderr_summary,
            stdout_hash=sha256_text(stdout_text),
            sanitizer_event_id=sanitized.audit_event_id,
        )

    def call(
        self,
        prompt: str,
        expect_json: bool = True,
        max_retries: int | None = None,  # noqa: U100
        response_max_tokens: int | None = None,  # noqa: U100
        response_retry_max_tokens: int | None = None,  # noqa: U100
    ) -> dict[str, Any]:
        result = self.run(prompt, expect_json=expect_json)
        if not result.ok:
            raise RuntimeError(f"AgentBackend failed: {result.status}")
        return result.data

    def _authorization_record(self):
        record = self._authorization_store.get_record(
            self.config.agent,
            directory=self.config.directory,
            capability="content_analysis",
            purpose="distillation",
        )
        if record is not None or self.config.directory == "*":
            return record
        return self._authorization_store.get_record(
            self.config.agent,
            directory="*",
            capability="content_analysis",
            purpose="distillation",
        )

    def _stderr_summary(self, text: str) -> str:
        if not text:
            return ""
        sanitized = self._sanitizer.sanitize(
            agent=self.config.agent,
            text=text,
            args=(),
            source_label="agent_backend_stderr",
            allowed_dirs=self.config.allowed_dirs,
        )
        summary = " ".join(sanitized.redacted_text.split())
        if len(summary) > _MAX_STDERR_SUMMARY_CHARS:
            return summary[: _MAX_STDERR_SUMMARY_CHARS - 3] + "..."
        return summary

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, AttributeError):
            proc.kill()

    def _result(
        self,
        *,
        status: str,
        ok: bool,
        started: float,
        exit_code: int | None = None,
        data: dict[str, Any] | None = None,
        failure_reason: str = "",
        stderr_summary: str = "",
        stdout_hash: str = "",
        sanitizer_event_id: str = "",
        findings: list[dict[str, str]] | None = None,
    ) -> AgentBackendResult:
        return AgentBackendResult(
            schema_version=RESULT_SCHEMA_VERSION,
            agent=self.config.agent,
            status=status,
            ok=ok,
            elapsed_ms=(time.monotonic() - started) * 1000,
            exit_code=exit_code,
            data=data or {},
            failure_reason=failure_reason,
            stderr_summary=stderr_summary,
            stdout_hash=stdout_hash,
            sanitizer_event_id=sanitizer_event_id,
            findings=findings or [],
        )
