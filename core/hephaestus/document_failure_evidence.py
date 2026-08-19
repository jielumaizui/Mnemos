"""Exact evidence binding for document-distillation failures."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from core.hephaestus.distillation_failure import record_distillation_failure


def build_document_failure_evidence(
    *,
    backend: Any,
    prompts: Iterable[str],
    responses: Iterable[Any],
    content: str,
    source_event_refs: list[str],
) -> tuple[str, dict[str, Any]]:
    """Return raw response bytes and immutable document-call bindings."""

    prompt_list = list(prompts)
    response_list = list(responses)
    raw_response = json.dumps(
        [str(response.raw_text) for response in response_list],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                prompt_list,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if prompt_list
        else "missing:prompt_hash"
    )
    visible_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    response_hash = (
        "sha256:" + hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
        if response_list
        else "missing:response_hash"
    )
    execution_spec_hash = "missing:execution_spec_hash"
    if prompt_list:
        try:
            identity_payload = json.dumps(
                {
                    "backend": backend.checkpoint_identity(),
                    "prompt_hash": prompt_hash,
                    "contract": "document_distillation_failure_evidence.v1",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            identity_payload = ""
        if identity_payload:
            execution_spec_hash = "sha256:" + hashlib.sha256(
                identity_payload.encode("utf-8")
            ).hexdigest()
    response_metadata = [response.to_failure_metadata() for response in response_list]
    latest = response_metadata[-1] if response_metadata else {}
    return raw_response, {
        "failure_path": "document_distillation",
        "execution_spec_hash": execution_spec_hash,
        "prompt_hash": prompt_hash,
        "visible_input_sha256": visible_hash,
        "response_hash": response_hash,
        "provider": latest.get("provider", ""),
        "model": latest.get("model", ""),
        "route": "document_distillation",
        "source_event_refs": source_event_refs,
        "responses": response_metadata,
    }


def record_document_provider_failure(
    *,
    session_id: str,
    content: str,
    metadata: dict[str, Any],
    database_dir: Any,
    source: str,
    error: BaseException,
) -> None:
    """Persist document transport/provider failure through the incident owner."""

    ref_keys = ("raw_event_id", "source_event_id", "provenance_id", "asset_id")
    source_refs = [str(metadata[key]) for key in ref_keys if str(metadata.get(key) or "").strip()]
    record_distillation_failure(
        session_id=session_id,
        fragments=[],
        validation_errors=[f"provider failure: {type(error).__name__}"],
        source=source,
        raw_response="",
        parse_metadata={
            "failure_path": "document_provider_failure",
            "visible_input_sha256": (
                "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            ),
            "source_event_refs": source_refs,
            "transport_empty": True,
        },
        database_dir=database_dir,
        producer="document_distillation",
    )


__all__ = ["build_document_failure_evidence", "record_document_provider_failure"]
