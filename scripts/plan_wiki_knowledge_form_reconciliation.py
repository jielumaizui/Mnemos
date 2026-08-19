#!/usr/bin/env python3
"""Build a read-only, hash-bound plan for historical Wiki knowledge forms."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_config
from core.frontmatter import fm_get
from core.kia.hygieia import KnowledgeImmuneSystem
from core.knowledge_form import display_knowledge_form

SCHEMA_VERSION = "mnemos.wiki_knowledge_form_reconciliation_plan.v1"

TEMPLATE_SIGNATURES = {
    "问题-解决": "遇到同类问题时，先对照适用场景确认是否命中",
    "决策记录": "做同类取舍时，优先看结论、适用场景和不适用边界",
    "经验法则": "把它当作经验规则使用：先检查适用边界",
    "反模式": "看到类似信号时优先暂停当前做法",
    "方法论": "按步骤执行，并在每一步记录输入、判断依据和输出结果",
    "洞察关联": "把它作为解释模型或判断视角使用",
}


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("unterminated frontmatter")
    payload = yaml.safe_load(text[4:end]) or {}
    if not isinstance(payload, dict):
        raise ValueError("frontmatter must be a mapping")
    return payload


def _checkpoint_forms(checkpoint_db: Path | None) -> dict[tuple[str, str], set[str]]:
    if checkpoint_db is None or not checkpoint_db.is_file():
        return {}
    forms: dict[tuple[str, str], set[str]] = {}
    with sqlite3.connect(
        f"file:{checkpoint_db}?mode=ro&immutable=1",
        uri=True,
    ) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' " "AND name='distill_chunk_results'"
        ).fetchone()
        if table is None:
            return {}
        rows = conn.execute(
            "SELECT session_id, fragment_json FROM distill_chunk_results "
            "WHERE status='completed'"
        )
        for session_id, raw_fragments in rows:
            try:
                fragments = json.loads(str(raw_fragments))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(fragments, list):
                continue
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                fragment_frontmatter = fragment.get("frontmatter") or {}
                if not isinstance(fragment_frontmatter, dict):
                    fragment_frontmatter = {}
                title = str(fragment.get("title") or fragment_frontmatter.get("名称") or "").strip()
                form = display_knowledge_form(fragment.get("form"))
                if title and form:
                    forms.setdefault((str(session_id), title), set()).add(form)
    return forms


def _reviewed_forms(review_manifest: Path | None) -> dict[str, dict[str, str]]:
    if review_manifest is None:
        return {}
    payload = json.loads(review_manifest.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "mnemos.wiki_knowledge_form_review.v1":
        raise ValueError("unsupported knowledge-form review manifest")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("knowledge-form review entries must be a list")
    result: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("knowledge-form review entry must be an object")
        relative_path = str(entry.get("relative_path") or "").strip()
        before_hash = str(entry.get("before_sha256") or "").strip()
        form = display_knowledge_form(entry.get("form"))
        evidence = str(entry.get("evidence") or "").strip()
        if not all((relative_path, before_hash, form, evidence)):
            raise ValueError("knowledge-form review entry is incomplete")
        assert form is not None
        if relative_path in result:
            raise ValueError(f"duplicate knowledge-form review: {relative_path}")
        result[relative_path] = {
            "before_sha256": before_hash,
            "form": form,
            "evidence": evidence,
        }
    return result


def build_plan(
    *,
    wiki_dir: Path,
    checkpoint_db: Path | None = None,
    review_manifest: Path | None = None,
) -> dict[str, Any]:
    wiki_dir = wiki_dir.expanduser().resolve(strict=True)
    checkpoint_index = _checkpoint_forms(checkpoint_db)
    review_index = _reviewed_forms(review_manifest)
    updates: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    already_covered = 0
    eligible = 0
    recovery_sources = {
        "template_signature": 0,
        "checkpoint": 0,
        "manual_review": 0,
    }
    for page in sorted(KnowledgeImmuneSystem(wiki_base=str(wiki_dir))._list_pages()):
        relative_path = str(page.relative_to(wiki_dir))
        raw = page.read_bytes()
        text = raw.decode("utf-8")
        try:
            frontmatter = _frontmatter(text)
        except (ValueError, yaml.YAMLError) as exc:
            unresolved.append({"relative_path": relative_path, "reason": f"parse_error:{exc}"})
            continue
        if fm_get(frontmatter, "distilled_at", "") in (None, ""):
            continue
        eligible += 1
        existing = display_knowledge_form(fm_get(frontmatter, "form", ""))
        if existing:
            already_covered += 1
            continue

        before_hash = _sha256_bytes(raw)
        evidence: dict[str, str] = {}
        candidate_forms: set[str] = set()
        signature_forms = {
            form for form, signature in TEMPLATE_SIGNATURES.items() if signature in text
        }
        if len(signature_forms) == 1:
            form = next(iter(signature_forms))
            candidate_forms.add(form)
            evidence["template_signature"] = TEMPLATE_SIGNATURES[form]
        elif len(signature_forms) > 1:
            evidence["template_signature_conflict"] = ",".join(sorted(signature_forms))

        session_id = str(fm_get(frontmatter, "source_session", "") or "").strip()
        title = str(fm_get(frontmatter, "name", "") or "").strip()
        checkpoint_forms = checkpoint_index.get((session_id, title), set())
        candidate_forms.update(checkpoint_forms)
        if checkpoint_forms:
            evidence["checkpoint"] = ",".join(sorted(checkpoint_forms))

        reviewed = review_index.get(relative_path)
        if reviewed:
            if reviewed["before_sha256"] != before_hash:
                evidence["manual_review_hash_mismatch"] = reviewed["before_sha256"]
            else:
                candidate_forms.add(reviewed["form"])
                evidence["manual_review"] = reviewed["evidence"]

        if len(candidate_forms) > 1 or "template_signature_conflict" in evidence:
            conflicts.append(
                {
                    "relative_path": relative_path,
                    "before_sha256": before_hash,
                    "candidate_forms": sorted(candidate_forms),
                    "evidence": evidence,
                }
            )
            continue
        if not candidate_forms:
            unresolved.append(
                {
                    "relative_path": relative_path,
                    "before_sha256": before_hash,
                    "session_id": session_id,
                    "title": title,
                    "reason": "no_exact_form_evidence",
                }
            )
            continue
        form = next(iter(candidate_forms))
        for source in recovery_sources:
            if source in evidence:
                recovery_sources[source] += 1
        updates.append(
            {
                "relative_path": relative_path,
                "before_sha256": before_hash,
                "form": form,
                "evidence": evidence,
            }
        )

    plan_payload = {
        "wiki_dir": str(wiki_dir),
        "updates": updates,
        "unresolved": unresolved,
        "conflicts": conflicts,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not unresolved and not conflicts,
        "apply_ready": bool(updates) and not unresolved and not conflicts,
        "wiki_dir": str(wiki_dir),
        "checkpoint_db": str(checkpoint_db) if checkpoint_db else "",
        "review_manifest": str(review_manifest) if review_manifest else "",
        "eligible_page_count": eligible,
        "already_covered_count": already_covered,
        "recoverable_count": len(updates),
        "unresolved_count": len(unresolved),
        "conflict_count": len(conflicts),
        "recovery_source_counts": recovery_sources,
        "plan_hash": _sha256_json(plan_payload),
        "updates": updates,
        "unresolved": unresolved,
        "conflicts": conflicts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--checkpoint-db", type=Path)
    parser.add_argument("--review-manifest", type=Path)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    try:
        report = build_plan(
            wiki_dir=args.wiki_dir or Path(config.wiki_dir),
            checkpoint_db=(
                args.checkpoint_db
                if args.checkpoint_db is not None
                else Path(config.database_dir) / "distillation_chunks.db"
            ),
            review_manifest=args.review_manifest,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error, yaml.YAMLError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "apply_ready": False,
            "error": str(exc),
        }
    display = report
    if args.summary and "updates" in report:
        display = {
            key: value
            for key, value in report.items()
            if key not in {"updates", "unresolved", "conflicts"}
        }
        display["update_examples"] = report["updates"][:5]
        display["unresolved_examples"] = report["unresolved"][:20]
        display["conflict_examples"] = report["conflicts"][:20]
    print(json.dumps(display, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
