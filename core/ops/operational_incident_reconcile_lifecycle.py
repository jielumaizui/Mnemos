"""Read helpers for idempotent legacy-reminder Wiki lifecycle recovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from core.wiki_projection_lifecycle import WikiProjectionLedger


def find_exact_move_receipt(
    ledger: WikiProjectionLedger,
    *,
    database_dir: Path,
    reminder_path: Path,
    archive_path: Path,
    content_hash: str,
) -> Any | None:
    receipt = next(
        (
            item
            for item in ledger.unpublished_mutations(limit=1000)
            if item.mutation_type == "move"
            and item.page_path == str(archive_path.resolve(strict=False))
            and item.previous_path == str(reminder_path.resolve(strict=False))
            and item.content_sha256 == content_hash.removeprefix("sha256:")
        ),
        None,
    )
    projection_db = database_dir / "wiki_projection.db"
    if receipt is None and projection_db.is_file():
        with sqlite3.connect(str(projection_db), timeout=10) as conn:
            row = conn.execute(
                """
                SELECT mutation_id FROM wiki_mutations
                WHERE mutation_type='move' AND page_path=? AND previous_path=?
                  AND content_sha256=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    str(archive_path.resolve(strict=False)),
                    str(reminder_path.resolve(strict=False)),
                    content_hash.removeprefix("sha256:"),
                ),
            ).fetchone()
        receipt = ledger.mutation_receipt(str(row[0])) if row else None
    return receipt


def find_source_create_receipt(
    ledger: WikiProjectionLedger,
    *,
    database_dir: Path,
    reminder_path: Path,
) -> Any | None:
    projection_db = database_dir / "wiki_projection.db"
    if not projection_db.is_file():
        return None
    identity = ledger.page_identity(reminder_path)
    if identity is None:
        return None
    with sqlite3.connect(str(projection_db), timeout=10) as conn:
        row = conn.execute(
            """
            SELECT mutation_id FROM wiki_mutations
            WHERE mutation_type='create' AND page_path=? AND page_id=?
            ORDER BY created_at LIMIT 1
            """,
            (str(reminder_path.resolve(strict=False)), str(identity["page_id"])),
        ).fetchone()
    return ledger.mutation_receipt(str(row[0])) if row else None


__all__ = ["find_exact_move_receipt", "find_source_create_receipt"]
