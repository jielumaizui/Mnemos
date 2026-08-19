"""Recovery helpers for incomplete trusted push writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from core.trust.models import JournalEventInput
from core.trust.proposal_queue import ProposalQueue
from core.trust.write_journal import WriteJournal


class TrustedPushRecovery:
    """Close prepare events that did not reach commit/rollback/abort."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._journal = WriteJournal(self.db_path)
        self._queue = ProposalQueue(self.db_path)

    def recover(self, *, apply: bool = False) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for event in self._journal.open_prepares():
            target = Path(event["target_uri"])
            event_type = "commit" if target.exists() else "abort"
            metadata = {"recovery": True}
            if apply:
                self._journal.append_event(
                    JournalEventInput(
                        proposal_id=event["proposal_id"],
                        event_type=event_type,
                        target_uri=event["target_uri"],
                        content_hash=event["content_hash"],
                        metadata=metadata,
                        actor="recovery",
                    )
                )
                status = "committed" if event_type == "commit" else "failed"
                error = "" if event_type == "commit" else "recovered prepare without target"
                try:
                    self._queue.update_status(event["proposal_id"], status, error)
                except KeyError:
                    pass
            results.append(
                {
                    "proposal_id": event["proposal_id"],
                    "event": event_type if apply else "planned_" + event_type,
                    "target_uri": event["target_uri"],
                    "applied": apply,
                }
            )
        return results
