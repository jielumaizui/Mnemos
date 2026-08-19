# -*- coding: utf-8 -*-
"""Persona application service used by the integration facade."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Mapping

from core.access_policy import AccessNarrowing, PrincipalEnvelope


class PersonaApplicationService:
    """Default implementation for persona-facing facade operations."""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)

    def record_explicit_profile_evidence(
        self,
        *,
        source_authority_catalog,
        source_authority_id: str,
        raw_db_path: str | Path,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        signal_type: str,
        dimension: str,
        quote: str,
        confidence: float = 0.5,
        assertion_id: str = "",
        expected_revision_id: str = "",
        signal_store=None,
    ) -> Dict:
        """Record one user-quoted Profile v2 fact through the sole producer.

        The lower write primitive verifies the exact authority catalog against
        canonical Raw before it can append either the signal or its assertion.
        This service is deliberately the only production caller of that
        primitive; direct ProfileSignal/Assertion APIs are retained solely for
        migrations, audits, and isolated fixtures.
        """

        if signal_store is None:
            from core.persona.psyche import get_signal_store

            signal_store = get_signal_store()
        return signal_store.record_authorized_profile_evidence(
            source_authority_catalog=source_authority_catalog,
            source_authority_id=source_authority_id,
            raw_db_path=str(raw_db_path),
            principal=principal,
            narrowing=narrowing,
            signal_type=signal_type,
            dimension=dimension,
            quote=quote,
            confidence=confidence,
            assertion_id=assertion_id,
            expected_revision_id=expected_revision_id,
        )

    def run_canonical_revision_cycle(
        self,
        *,
        signal_store,
        days: int = 30,
        analyzer=None,
        persona_store=None,
    ) -> Dict:
        """Analyze and commit through the sole runtime Persona writer owner.

        Adapters may schedule this application command but must not construct
        a ``PersonaStore`` or invoke ``save_persona`` directly.  The command
        preserves the existing no-op/materiality boundary.  A material
        candidate carries its exact unconsumed signal cursor into the
        canonical revision transaction; a no-op leaves that evidence pending.
        """

        if analyzer is None:
            from core.persona.pythia import PreferenceAnalyzer

            analyzer = PreferenceAnalyzer(signal_store)
        if persona_store is None:
            from core.persona.delphi import PersonaStore

            persona_store = PersonaStore(signal_store=signal_store)

        previous_profile, _ = persona_store.load_persona()
        prior = (
            previous_profile
            if previous_profile is not None and previous_profile.version > 0
            else None
        )
        profile = analyzer.analyze(
            days=days,
            previous_profile=prior,
            incremental=True,
        )
        if profile is None:
            return {"analyzed": False, "unchanged": True, "reason": "no_candidate"}
        if profile is previous_profile or (
            prior is not None and not analyzer.is_material_change(prior, profile)
        ):
            return {"analyzed": False, "unchanged": True, "version": profile.version}

        consume_signal_ids = self._canonical_signal_cursor(profile)
        if consume_signal_ids:
            persona_store.save_persona(profile, consume_signal_ids=consume_signal_ids)
        else:
            persona_store.save_persona(profile)
        return {"analyzed": True, "version": profile.version}

    @staticmethod
    def _canonical_signal_cursor(
        profile: object,
    ) -> dict[str, list[int]]:
        """Fail closed unless the candidate carries a concrete exact cursor."""

        source_signal_ids = getattr(profile, "source_signal_ids", None)
        if source_signal_ids is None:
            return {}
        if not isinstance(source_signal_ids, Mapping):
            raise ValueError("Persona candidate source cursor must be a mapping")
        from core.persona.psyche_material_contracts import canonical_persona_signal_cursor

        return canonical_persona_signal_cursor(source_signal_ids)

    @staticmethod
    def _restricted_profile_v2() -> Dict:
        """Return a body-free response when no profile ACL can be evaluated."""

        return {
            "schema_version": "mnemos.user_cognitive_profile.v2",
            "status": "restricted",
            "profile_assertions": [],
        }

    def _build_profile_v2(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        consumer: str = "",
    ) -> tuple[Dict, str]:
        """Build cognitive persona v2 through the per-assertion ACL seam."""

        if principal is None:
            return self._restricted_profile_v2(), ""
        try:
            from core.persona.psyche import get_signal_store

            profile, access = get_signal_store().build_authorized_user_cognitive_profile_v2(
                principal=principal,
                narrowing=narrowing,
                purpose=purpose,
                consumer=consumer,
            )
            if not access.get("authorized_count"):
                profile["status"] = "restricted"
                profile["access_filter"] = dict(access.get("denied_by_reason") or {})
            return profile, str(access.get("read_authorization_token") or "")
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            self._logger.debug("[persona] cognitive profile v2 build failed", exc_info=True)
            return self._restricted_profile_v2(), ""

    def _record_profile_usage(
        self,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing | None,
        consumer: str,
        matched_assertion_revisions: Dict[str, str],
        baseline_output: object,
        persona_enabled_output: object,
        outcome: str,
        read_authorization_token: str,
    ) -> None:
        from core.persona.psyche import ProfileUsageLog, get_signal_store
        from core.persona.profile_effect import compare_profile_effect

        if not matched_assertion_revisions:
            return
        get_signal_store().record_profile_usage(
            ProfileUsageLog(
                consumer=consumer,
                profile_fields_used=sorted(matched_assertion_revisions),
                read_purpose="persona_behavior_prompt",
                read_authorization_token=read_authorization_token,
                target_receipt=compare_profile_effect(
                    owner=consumer,
                    target_type="prompt",
                    target_id="persona_behavior_prompt",
                    matched_assertion_revisions=matched_assertion_revisions,
                    baseline_output=baseline_output,
                    persona_enabled_output=persona_enabled_output,
                    expected_delta={
                        "kind": "prompt_list",
                        "section": "behavior_prompts",
                        "rendered_assertion_revisions": dict(
                            sorted(matched_assertion_revisions.items())
                        ),
                    },
                ),
                outcome=outcome,
            ),
            principal=principal,
            narrowing=narrowing,
        )

    def persona_summary(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return only ACL-authorized persona state.

        PreferenceAnalyzer reads unreconciled signal tables, so it is
        intentionally excluded until a typed ACL reconciliation is complete.
        """

        if principal is None:
            return {
                "success": False,
                "code": "principal_required",
                "profile": {},
                "user_cognitive_profile_v2": self._restricted_profile_v2(),
            }
        profile_v2, _read_authorization_token = self._build_profile_v2(
            principal=principal,
            narrowing=narrowing,
            purpose="persona_summary_read",
        )
        return {
            "success": True,
            "profile": {},
            "user_cognitive_profile_v2": profile_v2,
        }

    def persona_behavior_prompt(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Render behavior guidance from authorized v2 assertions only."""

        if principal is None:
            return {
                "success": False,
                "code": "principal_required",
                "behavior_prompts": [],
                "onboarding_prompt": "",
                "user_cognitive_profile_v2": self._restricted_profile_v2(),
                "raw_profile": {},
            }
        profile_v2, read_authorization_token = self._build_profile_v2(
            principal=principal,
            narrowing=narrowing,
            purpose="persona_behavior_prompt",
            consumer="persona_behavior_prompt",
        )
        prompts: List[str] = []
        matched_assertion_revisions: Dict[str, str] = {}
        seen_claims: set[str] = set()
        for assertion in profile_v2.get("profile_assertions", []):
            claim = str(assertion.get("claim") or "").strip()
            normalized_claim = " ".join(claim.split())
            if not normalized_claim or normalized_claim in seen_claims:
                continue
            prompts.append(claim)
            seen_claims.add(normalized_claim)
            assertion_id = str(assertion.get("assertion_id") or "")
            revision_id = str(assertion.get("current_revision_id") or "")
            if not assertion_id or not revision_id:
                raise ValueError("behavior prompt assertion lacks immutable revision")
            matched_assertion_revisions[assertion_id] = revision_id
            if len(prompts) >= 6:
                break

        onboarding = self.load_onboarding_prompt()
        self._record_profile_usage(
            principal=principal,
            narrowing=narrowing,
            consumer="persona_behavior_prompt",
            matched_assertion_revisions=matched_assertion_revisions,
            baseline_output=[],
            persona_enabled_output=prompts,
            outcome="prompt_returned",
            read_authorization_token=read_authorization_token,
        )

        return {
            "success": True,
            "behavior_prompts": prompts,
            "onboarding_prompt": onboarding,
            "user_cognitive_profile_v2": profile_v2,
            "raw_profile": {},
        }

    def persona_behavior_metrics(
        self,
        days: int = 30,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return only ACL-authorized profile usage metrics.

        Historical BehaviorPromptTracker records do not carry object ACL lineage,
        so this endpoint deliberately reports their reconciliation state rather
        than opening them for a profile-metrics request.
        """

        if principal is None:
            return {"success": False, "code": "principal_required", "days": days}
        try:
            from core.persona.psyche import get_signal_store

            metrics = get_signal_store().get_authorized_profile_usage_metrics(
                days=days,
                principal=principal,
                narrowing=narrowing,
                purpose="persona_usage_metrics",
            )
            return {
                "success": True,
                "days": days,
                "tracking_status": "legacy_tracker_acl_unavailable",
                "profile_usage": metrics,
            }
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            self._logger.error("[persona_behavior_metrics] 获取指标失败: %s", exc)
            return {
                "success": False,
                "error": str(exc),
                "days": days,
                "total_calls": 0,
                "by_agent": {},
                "by_source": {},
                "by_strategy": {},
                "ab_test": {},
                "daily_calls": [],
            }

    def load_onboarding_prompt(self) -> str:
        """Load the host-agent onboarding prompt with connection status."""
        from core.diagnostics import ConnectionDiagnostics

        onboarding_path = Path(__file__).resolve().parents[2] / "prompts" / "agent_onboarding.md"
        if onboarding_path.exists():
            base = onboarding_path.read_text(encoding="utf-8")
        else:
            base = (
                "\n[Mnemos Onboarding]\n"
                "你是 Mnemos 的宿主 Agent。请帮用户完成以下连接任务：\n"
                "1. 调用 self_diagnose() 查看系统状态\n"
                "2. 确认 Wiki 路径，调用 configure_wiki(vault_path=...)\n"
                "3. 调用 detect_sources() 检查 Agent 数据源\n"
            )

        try:
            report = ConnectionDiagnostics.full_report()
            tasks = report.get("tasks", [])

            lines = ["\n[Mnemos 连接状态快照]"]
            pending_high = [
                task
                for task in tasks
                if task.get("priority") == "high" and not task.get("completed")
            ]
            pending_medium = [
                task
                for task in tasks
                if task.get("priority") == "medium" and not task.get("completed")
            ]

            if not pending_high and not pending_medium:
                lines.append("✓ 所有核心连接已就绪，Mnemos 完全在线。")
            else:
                if pending_high:
                    lines.append("🔴 高优先级待办:")
                    for task in pending_high:
                        lines.append(f"  • {task['task']}: {task['action']}")
                if pending_medium:
                    lines.append("🟡 中优先级待办:")
                    for task in pending_medium:
                        lines.append(f"  • {task['task']}: {task['action']}")

            return base + "\n" + "\n".join(lines) + "\n"
        # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            return base

    def signal_collect(self, sources: List[str] | None = None) -> Dict:
        """Collect persona signals."""
        from core.persona.daimon import SignalCollector

        collector = SignalCollector()
        results = collector.collect_all(sources=sources)
        return {
            "success": True,
            "results": results,
        }

    def persona_update(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Refresh persona signals and return only an authorized v2 profile."""
        from core.persona.daimon import SignalCollector
        from core.persona.pythia import PreferenceAnalyzer

        if principal is None:
            return {
                "success": False,
                "code": "principal_required",
                "profile": {},
                "user_cognitive_profile_v2": self._restricted_profile_v2(),
            }
        try:
            collector = SignalCollector()
            collect_result = collector.collect_all()

            analyzer = PreferenceAnalyzer()
            analyzer.analyze(days=30)
            profile_v2, _read_authorization_token = self._build_profile_v2(
                principal=principal,
                narrowing=narrowing,
                purpose="persona_summary_read",
            )

            return {
                "success": True,
                "message": "画像更新完成",
                "signals_collected": collect_result,
                "profile": {},
                "user_cognitive_profile_v2": profile_v2,
            }
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            self._logger.error("画像更新失败: %s", exc)
            return {
                "success": False,
                "message": f"更新失败: {exc}",
            }
