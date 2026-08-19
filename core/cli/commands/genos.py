"""Genos/DNA command for Mnemos CLI."""

from __future__ import annotations

import json
from typing import Any

from core.kia.genos import (
    DNAEngine,
    KnowledgeDNA,
    SimilarityResult,
    check_duplicate,
    compute_and_save,
)


def _similarity_to_dict(result: SimilarityResult) -> dict[str, Any]:
    return {
        "target_page": result.target_page,
        "overall_score": result.overall_score,
        "dimension_scores": result.dimension_scores,
        "verdict": result.verdict,
        "reason": result.reason,
    }


def _print_duplicate_results(page_path: str, results: list[SimilarityResult]) -> None:
    print(f"重复检查: {page_path}")
    if not results:
        print("未发现重复页面")
        return

    print(f"发现 {len(results)} 个候选:")
    for index, result in enumerate(results, 1):
        print(f"  {index}. [{result.overall_score:.2f}] {result.target_page} ({result.verdict})")
        if result.reason:
            print(f"     原因: {result.reason}")


def _print_compute_result(page_path: str, dna: KnowledgeDNA | None) -> None:
    print(f"DNA 计算: {page_path}")
    if dna is None:
        print("未能计算 DNA")
        return

    print(f"已保存 DNA: {dna.page_path}")
    print(f"  语义签名: {dna.semantic_signature or '-'}")
    print(f"  领域/类型: {dna.domain or '-'} / {dna.knowledge_type or '-'}")
    print(f"  置信度: {dna.confidence:.2f}")


def _cmd_compute(args: Any) -> int:
    engine = DNAEngine(wiki_base=args.wiki_base) if args.wiki_base else None
    dna = compute_and_save(args.page, engine=engine)
    if getattr(args, "json", False):
        payload = {
            "page_path": args.page,
            "computed": dna is not None,
            "dna": dna.to_dict() if dna is not None else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_compute_result(args.page, dna)
    return 0 if dna is not None else 1


def _cmd_duplicate(args: Any) -> int:
    engine = DNAEngine(wiki_base=args.wiki_base) if args.wiki_base else None
    results = check_duplicate(args.page, engine=engine)
    if getattr(args, "json", False):
        payload = {
            "page_path": args.page,
            "count": len(results),
            "duplicates": [_similarity_to_dict(result) for result in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_duplicate_results(args.page, results)
    return 0


def cmd_genos(args: Any) -> int:
    """Handle Genos/DNA subcommands."""
    if getattr(args, "genos_cmd", "") == "compute":
        return _cmd_compute(args)
    if getattr(args, "genos_cmd", "") == "duplicate":
        return _cmd_duplicate(args)

    print("用法: mnemos genos {compute,duplicate} <page> [--wiki-base PATH] [--json]")
    return 2
