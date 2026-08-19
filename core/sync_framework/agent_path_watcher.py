"""Agent data-path watcher that refreshes discovery state without reading content."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List

from core.sync_framework.registry import PathDiscover


@dataclass(frozen=True)
class AgentPathState:
    agent: str
    path: str
    exists: bool
    mtime: float | None
    availability_state: str = "unknown"
    error_code: str = ""
    changed: bool = False


class AgentPathWatcher:
    """Track discovered Agent data paths using only path metadata."""

    def __init__(
        self,
        agents: Iterable[str],
        discoverer: Callable[[str], Path | None] | None = None,
        enabled: bool = True,
    ):
        self.agents = tuple(agents)
        self.discoverer = discoverer or PathDiscover.find
        self.enabled = enabled
        self._state: Dict[str, AgentPathState] = {}

    def refresh(self) -> List[AgentPathState]:
        if not self.enabled:
            return []

        changed: List[AgentPathState] = []
        next_state: Dict[str, AgentPathState] = {}
        for agent in self.agents:
            path = self.discoverer(agent)
            if path is not None:
                path = PathDiscover.resolve_agent_subdir(agent, path)
            state = self._state_for(agent, path)
            previous = self._state.get(agent)
            is_changed = previous is None or (
                previous.path,
                previous.exists,
                previous.mtime,
                previous.availability_state,
                previous.error_code,
            ) != (
                state.path,
                state.exists,
                state.mtime,
                state.availability_state,
                state.error_code,
            )
            state = AgentPathState(
                agent=state.agent,
                path=state.path,
                exists=state.exists,
                mtime=state.mtime,
                availability_state=state.availability_state,
                error_code=state.error_code,
                changed=is_changed,
            )
            next_state[agent] = state
            if is_changed:
                changed.append(state)
        self._state = next_state
        return changed

    @staticmethod
    def _state_for(agent: str, path: Path | None) -> AgentPathState:
        if path is None:
            return AgentPathState(
                agent=agent,
                path="",
                exists=False,
                mtime=None,
                availability_state="missing",
            )
        expanded = Path(path).expanduser()
        try:
            metadata = expanded.stat()
            return AgentPathState(
                agent=agent,
                path=str(expanded),
                exists=True,
                mtime=metadata.st_mtime,
                availability_state="available",
            )
        except FileNotFoundError:
            return AgentPathState(
                agent=agent,
                path=str(expanded),
                exists=False,
                mtime=None,
                availability_state="missing",
            )
        except OSError:
            return AgentPathState(
                agent=agent,
                path=str(expanded),
                exists=False,
                mtime=None,
                availability_state="unavailable",
                error_code="agent_path_inspection_unavailable",
            )
