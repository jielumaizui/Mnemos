# -*- coding: utf-8 -*-
"""Observation application service used by the integration facade."""

from __future__ import annotations

from datetime import datetime
from typing import Dict

from core.access_policy import AccessNarrowing, PrincipalEnvelope


class ObservationApplicationService:
    """Default implementation for observation-facing facade operations."""

    def observation_run(self, full: bool = False, since: str = "") -> Dict:
        """Run the L3 observation engine."""
        from core.cognitive.observation_engine import (
            ObservationEngine,
            canonical_raw_engine_kwargs,
        )
        from core.config import get_config

        cfg = get_config()
        engine = ObservationEngine(
            wiki_dir=str(cfg.wiki_dir),
            **canonical_raw_engine_kwargs(cfg),
        )

        if full:
            batch = engine.run(persist=True)
        elif since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError as exc:
                return {"success": False, "error": f"invalid since: {exc}"}
            batch = engine.run_incremental(since=since_dt, persist=True)
        else:
            batch = engine.run(persist=True)

        return {
            "success": True,
            "observations": batch.total_observations,
            "dimensions": len(batch.dimension_counts),
            "dimension_counts": dict(batch.dimension_counts),
        }

    def observation_search(
        self,
        dimension: str = "",
        source_type: str = "",
        limit: int = 20,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "observation_read",
    ) -> Dict:
        """Search the L3 observation index."""
        if principal is None:
            return {
                "success": False,
                "error_code": "principal_required",
                "count": 0,
                "observations": [],
                "access": {
                    "candidate_count": 0,
                    "authorized_count": 0,
                    "denied_by_reason": {"principal_required": 1},
                },
            }
        from core.cognitive.models import Dimension, SourceType
        from core.cognitive.observation_store import ObservationIndex

        index = ObservationIndex()
        dim = None
        if dimension:
            try:
                dim = Dimension(dimension)
            except ValueError:
                return {"success": False, "error": f"unknown dimension: {dimension}"}

        st = None
        if source_type:
            try:
                st = SourceType(source_type)
            except ValueError:
                return {"success": False, "error": f"unknown source_type: {source_type}"}

        observations, access = index.authorized_query(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
            dimension=dim,
            source_type=st,
            limit=limit,
        )
        return {
            "success": True,
            "count": len(observations),
            "access": access,
            "observations": [
                {
                    "id": observation.id,
                    "dimension": observation.dimension.value,
                    "observation_type": observation.observation_type.value,
                    "value": observation.value,
                    "confidence": observation.confidence,
                    "source_type": observation.source_type.value,
                    "source_path": observation.source_path,
                    "source_id": observation.source_id,
                    "evidence": observation.evidence[:3],
                    "observed_at": (
                        observation.observed_at.isoformat() if observation.observed_at else ""
                    ),
                }
                for observation in observations
            ],
        }
