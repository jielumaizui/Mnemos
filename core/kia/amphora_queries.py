"""Read-only queue queries for the Amphora storage façade."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AmphoraQueries:
    """Expose queue read models through injected storage ownership."""

    connect: Callable[[], Any]
    init_db: Callable[[], None]
    row_to_dict: Callable[[Any], dict[str, Any]]

    def list_processing(self) -> list[dict[str, Any]]:
        """Return processing tasks in claim order."""

        self.init_db()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM distillation_tasks
                WHERE status = 'processing'
                ORDER BY started_at ASC
                """
            ).fetchall()
            return [self.row_to_dict(row) for row in rows]

    def task_count(self, status: str | None = None) -> int:
        """Return a status-aware queue count for monitoring."""

        self.init_db()
        with self.connect() as connection:
            if status == "done":
                return int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM distillation_tasks
                        WHERE status IN ('committed', 'intentional_skip')
                        """
                    ).fetchone()[0]
                )
            if status:
                row = connection.execute(
                    "SELECT COUNT(*) FROM distillation_tasks WHERE status = ?",
                    (status,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM distillation_tasks"
                ).fetchone()
            return int(row[0]) if row else 0


__all__ = ["AmphoraQueries"]
