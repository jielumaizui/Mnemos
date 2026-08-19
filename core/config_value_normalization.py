"""Compatibility normalization for values that cannot safely be restored."""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


def discard_non_restorable_capture_values(
    file_data: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Drop obsolete values whose historical semantics cannot be restored.

    The untouched source document remains available to the explicit migration
    planner; this helper only produces the strict runtime view.
    """
    if not isinstance(file_data, dict):
        return file_data, ()
    sanitized = copy.deepcopy(file_data)
    ignored: list[str] = []
    capture = sanitized.get("capture")
    if isinstance(capture, dict) and "duplicate_ttl_days" in capture:
        capture.pop("duplicate_ttl_days", None)
        ignored.append("capture.duplicate_ttl_days")
        logger.warning(
            "忽略已移除的 capture.duplicate_ttl_days；Capture 幂等已永久绑定 canonical Raw revision"
        )
    raw_projection = sanitized.get("raw_projection")
    max_turn_chars = (
        raw_projection.get("max_turn_chars") if isinstance(raw_projection, dict) else None
    )
    if (
        isinstance(raw_projection, dict)
        and isinstance(max_turn_chars, int)
        and not isinstance(max_turn_chars, bool)
        and max_turn_chars
    ):
        raw_projection.pop("max_turn_chars", None)
        ignored.append("raw_projection.max_turn_chars")
        logger.warning(
            "忽略 raw_projection.max_turn_chars；canonical Raw 必须无截断，"
            "紧凑输出须使用独立 Raw Preview"
        )
    distill = sanitized.get("distill")
    if isinstance(distill, dict) and "skill_suggestion_max_chars" in distill:
        distill.pop("skill_suggestion_max_chars", None)
        ignored.append("distill.skill_suggestion_max_chars")
        logger.warning(
            "忽略已移除的 distill.skill_suggestion_max_chars；"
            "COG-013 proposal 必须从完整认知资产派生"
        )
    if isinstance(distill, dict) and "max_collect_per_cycle" in distill:
        distill.pop("max_collect_per_cycle", None)
        ignored.append("distill.max_collect_per_cycle")
        logger.warning(
            "忽略已移除的 distill.max_collect_per_cycle；" "COG-028 起同步 worker 是唯一蒸馏入口"
        )
    daemon = sanitized.get("daemon")
    services = daemon.get("services") if isinstance(daemon, dict) else None
    if isinstance(services, dict) and "persona_extensions" in services:
        services.pop("persona_extensions", None)
        ignored.append("daemon.services.persona_extensions")
        logger.warning(
            "忽略已移除的 daemon.services.persona_extensions；"
            "COG-032 只允许带真实决策上下文的 persona_challenge 服务"
        )
    return sanitized, tuple(ignored)
