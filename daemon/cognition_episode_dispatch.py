"""Daemon bootstrap for the committed cognition-episode durable dispatcher."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register_cognition_episode_dispatch(
    event_bus: Any,
    config: Any | None,
    *,
    cognitive_graph_store: Any = None,
) -> Any:
    """Register consumers and enqueue only revisions with pending commands."""

    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.config import get_config

    config = config or get_config()
    owner = CognitionEpisodeDispatchOwner(
        config=config,
        event_bus=event_bus,
        cognitive_graph_store=cognitive_graph_store,
    )
    owner.subscribe()
    pending = owner.publish_pending(
        limit=int(config.get("cognition_episode.dispatch_startup_limit", 100))
    )
    logger.info(
        "cognition episode durable dispatch registered; pending revisions=%d",
        pending["published"],
    )
    return owner
