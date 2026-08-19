# -*- coding: utf-8 -*-
"""Security helpers for document processing entrypoints."""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.privacy.ingestion_security import (
    attach_security_fields,
    assess_ingestion_security,
)


def blocked_document_distillation_result(doc: Any) -> Optional[Dict[str, Any]]:
    """Compatibility surface: content risk no longer deletes local Raw input."""
    del doc
    return None


def attach_document_security_fields(payload: Dict[str, Any], doc: Any) -> Dict[str, Any]:
    """Attach common document security metadata to a result payload."""
    security = assess_ingestion_security(getattr(doc, "content", "") or "")
    return attach_security_fields(payload, security)
