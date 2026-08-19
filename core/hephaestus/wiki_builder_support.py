"""Pure parsing and MOC selection helpers for the Wiki builder."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.frontmatter import fm_get, parse_frontmatter
from core.ops.durable_io import read_native_bytes_with_metadata

logger = logging.getLogger(__name__)


def parse_record_time(record: Dict) -> Optional[datetime]:
    """Extract a record timestamp from createTime or visible frontmatter."""

    create_time = record.get("createTime", "")
    if create_time:
        try:
            return datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("record time parse failed: %s", create_time, exc_info=True)
    content = record.get("content", "")
    date_match = re.search(
        r'^date:\s*"?(\d{4}-\d{2}-\d{2})"?',
        content,
        re.MULTILINE,
    )
    time_match = re.search(
        r'^time:\s*"?([0-9:]+)"?',
        content,
        re.MULTILINE,
    )
    if date_match:
        date_str = date_match.group(1)
        time_str = time_match.group(1) if time_match else "00:00"
        if len(time_str) == 4 and ":" not in time_str:
            time_str = f"{time_str[:2]}:{time_str[2:4]}"
        try:
            return datetime.fromisoformat(f"{date_str}T{time_str}")
        except ValueError:
            logger.warning("frontmatter time parse failed", exc_info=True)
    return None


def parse_float(value: object) -> float:
    """Parse a numeric MOC field without propagating malformed metadata."""

    if not isinstance(value, (str, int, float)):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_page_descriptor(md_file: Path, wiki_dir: Path) -> Optional[Dict]:
    """Return the bounded metadata projection used by generated MOCs."""

    relative_path = str(md_file.relative_to(wiki_dir))
    try:
        content_bytes, metadata = read_native_bytes_with_metadata(md_file)
        content = content_bytes.decode("utf-8")
    except (OSError, IOError, UnicodeError):
        return None
    frontmatter, _body = parse_frontmatter(content)
    if frontmatter is None:
        frontmatter = {}
    return {
        "rel_path": relative_path,
        "stem": md_file.stem,
        "mtime": metadata.st_mtime,
        "confidence": parse_float(fm_get(frontmatter, "confidence", 0.5)),
        "coverage": fm_get(frontmatter, "coverage", ""),
        "status": fm_get(frontmatter, "verification", ""),
        "heat_level": fm_get(frontmatter, "heat_level", ""),
        "heat_score": parse_float(fm_get(frontmatter, "heat_score", 0)),
        "summary": frontmatter.get(
            "摘要",
            frontmatter.get("summary", ""),
        ),
    }


def filter_recent_pages(
    pages: List[Dict],
    now: float,
    window_seconds: float,
) -> List[Dict]:
    """Order pages updated in the exact recent-time window."""

    recent = [page for page in pages if (now - page["mtime"]) < window_seconds]
    recent.sort(key=lambda page: page["mtime"], reverse=True)
    return recent


def filter_hot_pages(pages: List[Dict]) -> List[Dict]:
    """Order pages with explicit heat evidence."""

    hot = [
        page
        for page in pages
        if page.get("heat_score", 0) > 0 or page.get("heat_level") in ("warm", "hot")
    ]
    hot.sort(key=lambda page: page["heat_score"], reverse=True)
    return hot


def filter_pending_pages(pages: List[Dict]) -> List[Dict]:
    """Order pages whose verification or coverage remains pending."""

    pending = [
        page
        for page in pages
        if page.get("status") == "pending-verification" or page.get("coverage") == "partial"
    ]
    pending.sort(key=lambda page: page["confidence"])
    return pending


def filter_low_confidence_pages(pages: List[Dict]) -> List[Dict]:
    """Order pages below the explicit confidence threshold."""

    low_confidence = [page for page in pages if page["confidence"] < 0.5]
    low_confidence.sort(key=lambda page: page["confidence"])
    return low_confidence
