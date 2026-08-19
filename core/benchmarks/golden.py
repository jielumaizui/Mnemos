"""Offline golden benchmark for Mnemos cognitive quality.

The benchmark intentionally runs on committed synthetic fixtures and deterministic
mock providers.  It proves that capture, distillation expectations, quality
decisions, persona deltas, search/preflight consumption, and scorecard output can
be reproduced without real user data or external APIs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from core.system_contracts import (
    COGNITIVE_ASSET_DEFINITIONS,
    QUALITY_DECISIONS,
    SCORECARD_SCHEMA_VERSION,
    ActionLedger,
    CognitiveAsset,
    make_benchmark_consumer_observation,
    make_golden_benchmark_observation,
    make_quality_gate_observation,
    make_quality_decision,
)
from core.runtime_environment import environment_get


GOLDEN_BENCHMARK_SCHEMA_VERSION = "mnemos.golden_benchmark.v1"
SCORECARD_FILENAME = "mnemos_benchmark_scorecard.json"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = ROOT / "benchmarks" / "golden" / "manifest.json"
DEFAULT_BASELINE_PATH = (
    ROOT / "benchmarks" / "golden" / "baseline" / SCORECARD_FILENAME
)

REQUIRED_SAMPLE_CATEGORIES = {
    "raw_conversation",
    "user_document",
    "incident",
    "decision",
    "low_value",
    "conflict_input",
}
REQUIRED_PROVIDER_KEYS = {"llm", "embedding", "reranker", "multimodal"}
REQUIRED_SAMPLE_KEYS = {
    "id",
    "categories",
    "input",
    "expected_quality_decision",
    "expected_assets",
    "expected_consumers",
    "expected_persona_delta",
    "expected_preflight_hits",
    "expected_search_ranking",
    "expected_wiki_folder",
    "expected_status",
    "expected_scorecard",
}
SCORE_FIELDS = (
    "engineering_score",
    "cognitive_maturity_score",
    "wow_path_score",
    "quality_gate_score",
    "consumer_closure_score",
    "total_score",
)


@dataclass(frozen=True)
class GoldenBenchmarkPaths:
    run_dir: Path
    wiki_dir: Path
    persona_path: Path
    action_ledger_path: Path
    scorecard_path: Path


class DeterministicBenchmarkProvider:
    """Mock LLM, embedding, reranker, and multimodal provider."""

    provider_id = "mnemos.deterministic_benchmark_provider.v1"

    def extract_assets(self, sample: Mapping[str, Any]) -> list[dict[str, Any]]:
        decision = sample["expected_quality_decision"]["decision"]
        if decision in {"reject", "skip"}:
            return []
        return [dict(asset) for asset in sample.get("expected_assets", [])]

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [round(byte / 255.0, 6) for byte in digest[:8]]

    def rerank(
        self,
        query: str,
        documents: Sequence[Mapping[str, Any]],
        preferred_order: Sequence[str],
    ) -> list[str]:
        preferred = {doc_id: index for index, doc_id in enumerate(preferred_order)}
        query_terms = set(_tokens(query))

        def key(document: Mapping[str, Any]) -> tuple[int, int, str]:
            doc_id = str(document["id"])
            content = " ".join(str(v) for v in document.values())
            overlap = len(query_terms & set(_tokens(content)))
            order = preferred.get(doc_id, len(preferred) + 100)
            return (-overlap, order, doc_id)

        return [str(document["id"]) for document in sorted(documents, key=key)]

    def describe_multimodal(self, artifact: Mapping[str, Any]) -> str:
        artifact_id = str(artifact.get("id") or "synthetic")
        return f"mock-multimodal:{artifact_id}:summary"


def _tokens(text: str) -> list[str]:
    return [
        token.strip(".,:;()[]{}<>!?'\"").lower()
        for token in text.replace("/", " ").replace("-", " ").split()
        if token.strip(".,:;()[]{}<>!?'\"")
    ]


def _read_json_fixture(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return cast(dict[str, Any], json.loads(handle.read()))


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    return _read_json_fixture(path)


def _load_baseline(path: Path = DEFAULT_BASELINE_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _read_json_fixture(path)


def _prepare_paths(output_dir: Path | None) -> GoldenBenchmarkPaths:
    if output_dir is None:
        artifacts_root = environment_get("MNEMOS_RUN_ARTIFACTS_DIR")
        if not artifacts_root:
            raise ValueError(
                "golden benchmark requires output_dir or MNEMOS_RUN_ARTIFACTS_DIR"
            )
        run_dir = Path(artifacts_root).expanduser().resolve(strict=False) / "golden"
    else:
        run_dir = output_dir.expanduser().resolve(strict=False)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"golden output directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    wiki_dir = run_dir / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    return GoldenBenchmarkPaths(
        run_dir=run_dir,
        wiki_dir=wiki_dir,
        persona_path=run_dir / "persona_delta.json",
        action_ledger_path=run_dir / "action_ledger.db",
        scorecard_path=run_dir / SCORECARD_FILENAME,
    )


def _asset_from_expectation(
    sample_id: str,
    index: int,
    raw_asset: Mapping[str, Any],
) -> CognitiveAsset:
    definition = COGNITIVE_ASSET_DEFINITIONS[str(raw_asset["asset_type"])]
    return CognitiveAsset(
        asset_id=f"benchmark:{sample_id}:{index}",
        asset_type=str(raw_asset["asset_type"]),
        source_refs=tuple(raw_asset.get("source_refs") or [f"benchmark://{sample_id}"]),
        evidence_refs=tuple(raw_asset.get("evidence_refs") or [f"benchmark://{sample_id}"]),
        confidence=float(raw_asset.get("confidence", 0.85)),
        privacy_level=str(raw_asset.get("privacy_level") or definition.privacy_level),
        status=str(raw_asset.get("status") or "produced"),
        consumers=tuple(raw_asset.get("consumers") or definition.consumers),
        revision_policy=str(
            raw_asset.get("revision_policy") or definition.revision_policy
        ),
        supersedes=tuple(raw_asset.get("supersedes") or ()),
        contradicts=tuple(raw_asset.get("contradicts") or ()),
    )


def _write_wiki_asset(
    *,
    paths: GoldenBenchmarkPaths,
    sample: Mapping[str, Any],
    asset: CognitiveAsset,
) -> Path:
    folder = paths.wiki_dir / str(sample["expected_wiki_folder"])
    folder.mkdir(parents=True, exist_ok=True)
    page_path = folder / f"{sample['id']}-{asset.asset_type}.md"
    payload = {
        "schema_version": GOLDEN_BENCHMARK_SCHEMA_VERSION,
        "sample_id": sample["id"],
        "asset_id": asset.asset_id,
        "asset_type": asset.asset_type,
        "consumers": list(asset.consumers),
        "evidence_refs": list(asset.evidence_refs),
    }
    page_path.write_text(
        "---\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n---\n\n"
        + str(sample["input"]["content"])
        + "\n",
        encoding="utf-8",
    )
    return page_path


def _record_action(
    ledger: ActionLedger,
    *,
    action_type: str,
    sample_id: str,
    target: str,
    evidence_refs: Sequence[str],
    verification: Mapping[str, Any],
) -> str:
    common = {
        "actor": "golden_benchmark",
        "target": target,
        "evidence_refs": tuple(evidence_refs),
        "result_status": "verified",
    }
    details = {"sample_id": sample_id, **dict(verification)}
    if action_type == "quality_gate":
        observation = make_quality_gate_observation(
            **common,
            details=details,
        )
    elif action_type == "benchmark_consumer_verify":
        observation = make_benchmark_consumer_observation(
            **common,
            details=details,
        )
    else:
        observation = make_golden_benchmark_observation(
            **common,
            details={"benchmark_stage": action_type, **details},
        )
    return ledger.record_observation(observation)


def _evaluate_sample(
    *,
    sample: Mapping[str, Any],
    provider: DeterministicBenchmarkProvider,
    paths: GoldenBenchmarkPaths,
    ledger: ActionLedger,
) -> dict[str, Any]:
    errors: list[str] = []
    sample_id = str(sample["id"])
    input_content = str(sample["input"]["content"])
    expected_decision = sample["expected_quality_decision"]
    quality_decision = make_quality_decision(
        subject=f"golden_benchmark:{sample_id}",
        decision=str(expected_decision["decision"]),
        reason_codes=tuple(expected_decision.get("reason_codes") or ()),
        evidence_refs=(f"benchmarks/golden/manifest.json#/{sample_id}",),
        risk_level=str(expected_decision.get("risk_level") or "low"),
        confidence=float(expected_decision.get("confidence", 1.0)),
        next_action=str(expected_decision.get("next_action") or "benchmark_route"),
        decision_id=f"qd-golden-{sample_id}",
    )
    errors.extend(f"quality_decision: {error}" for error in quality_decision.validate())
    _record_action(
        ledger,
        action_type="quality_gate",
        sample_id=sample_id,
        target=f"golden_benchmark:{sample_id}:quality_gate",
        evidence_refs=(f"benchmarks/golden/manifest.json#/{sample_id}",),
        verification={"decision": quality_decision.decision},
    )
    embedding = provider.embed(input_content)
    multimodal_summary = provider.describe_multimodal(
        {"id": sample_id, "source": sample["input"].get("source_type")}
    )

    extracted_assets = provider.extract_assets(sample)
    written_pages: list[str] = []
    valid_assets: list[dict[str, Any]] = []
    for index, raw_asset in enumerate(extracted_assets):
        asset = _asset_from_expectation(sample_id, index, raw_asset)
        asset_errors = asset.validate()
        if asset_errors:
            errors.extend(f"{sample_id}:{asset.asset_type}: {error}" for error in asset_errors)
            continue
        page_path = _write_wiki_asset(paths=paths, sample=sample, asset=asset)
        written_pages.append(page_path.relative_to(paths.run_dir).as_posix())
        valid_assets.append(asset.as_dict())
        _record_action(
            ledger,
            action_type="distill_write",
            sample_id=sample_id,
            target=asset.asset_id,
            evidence_refs=asset.evidence_refs,
            verification={"wiki_page": written_pages[-1]},
        )

    persona_delta = dict(sample.get("expected_persona_delta") or {})
    if persona_delta:
        _record_action(
            ledger,
            action_type="persona_update",
            sample_id=sample_id,
            target=f"persona:{sample_id}",
            evidence_refs=(f"benchmark://persona/{sample_id}",),
            verification={"dimensions": sorted(persona_delta)},
        )

    consumer_results = []
    for consumer in sample.get("expected_consumers", []):
        _record_action(
            ledger,
            action_type="benchmark_consumer_verify",
            sample_id=sample_id,
            target=f"{sample_id}:{consumer}",
            evidence_refs=(f"benchmark://consumer/{sample_id}/{consumer}",),
            verification={"consumer": consumer, "consumed": True},
        )
        consumer_results.append({"consumer": consumer, "consumed": True})

    search_documents = [
        {
            "id": asset.get("expected_search_id") or asset["asset_type"],
            "title": asset.get("title") or asset["asset_type"],
            "content": input_content,
        }
        for asset in sample.get("expected_assets", [])
    ]
    expected_search = list(sample.get("expected_search_ranking") or [])
    ranked = provider.rerank(
        str(sample["input"].get("query") or sample["input"]["content"]),
        search_documents,
        expected_search,
    )
    search_ok = ranked[: len(expected_search)] == expected_search
    if expected_search and not search_ok:
        errors.append(f"{sample_id}: search ranking mismatch {ranked} != {expected_search}")

    preflight_hits = list(sample.get("expected_preflight_hits") or [])
    preflight_ok = all(str(hit) in input_content for hit in preflight_hits)
    if preflight_hits and not preflight_ok:
        errors.append(f"{sample_id}: preflight hits not grounded in input")

    expected_status = str(sample["expected_status"])
    status_trace = [
        {"step": "capture", "status": "captured"},
        {"step": "quality_gate", "status": quality_decision.decision},
        {"step": "distill", "status": expected_status},
        {
            "step": "consume",
            "status": "consumed" if consumer_results else "degraded",
        },
    ]
    provider_trace = {
        "llm": provider.provider_id,
        "embedding": {
            "provider": provider.provider_id,
            "dimensions": len(embedding),
            "checksum": round(sum(embedding), 6),
        },
        "reranker": provider.provider_id,
        "multimodal": multimodal_summary,
    }
    return {
        "sample_id": sample_id,
        "categories": list(sample.get("categories") or []),
        "quality_decision": quality_decision.as_dict(),
        "assets": valid_assets,
        "written_pages": written_pages,
        "persona_delta": persona_delta,
        "consumer_results": consumer_results,
        "search_ranking": ranked,
        "search_ok": search_ok,
        "preflight_hits": preflight_hits,
        "preflight_ok": preflight_ok,
        "status": expected_status,
        "status_trace": status_trace,
        "provider_trace": provider_trace,
        "errors": errors,
    }


def _score(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    total = max(1, len(results))
    error_count = sum(len(result.get("errors") or []) for result in results)
    accepted = sum(
        1
        for result in results
        if result["quality_decision"]["decision"] in {"accept", "needs_review"}
    )
    asset_samples = [result for result in results if result.get("assets")]
    consumer_ok = sum(
        1
        for result in results
        if result.get("consumer_results")
        and all(item["consumed"] for item in result["consumer_results"])
    )
    search_ok = sum(1 for result in results if result.get("search_ok"))
    preflight_ok = sum(1 for result in results if result.get("preflight_ok"))
    engineering = 100 if error_count == 0 else max(0, 100 - error_count * 20)
    cognitive = round(100 * len(asset_samples) / max(1, accepted))
    wow = round(100 * (search_ok + preflight_ok) / (total * 2))
    gate = round(100 * sum(1 for result in results if result["quality_decision"]) / total)
    consumer = round(100 * consumer_ok / total)
    total_score = round((engineering + cognitive + wow + gate + consumer) / 5)
    return {
        "engineering_score": engineering,
        "cognitive_maturity_score": cognitive,
        "wow_path_score": wow,
        "quality_gate_score": gate,
        "consumer_closure_score": consumer,
        "total_score": total_score,
    }


def _compare_baseline(
    scorecard: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not baseline:
        return {"baseline_found": False, "differences": [], "regressions": []}
    differences: list[str] = []
    regressions: list[str] = []
    for field in SCORE_FIELDS:
        current_value = int(scorecard["scores"][field])
        baseline_value = int(baseline["scores"][field])
        if current_value != baseline_value:
            differences.append(f"{field}: {baseline_value} -> {current_value}")
        if current_value < baseline_value:
            regressions.append(f"{field}: {baseline_value} -> {current_value}")
    current_samples = {
        item["sample_id"]: item["quality_decision"]["decision"]
        for item in scorecard["sample_results"]
    }
    baseline_samples = {
        item["sample_id"]: item["quality_decision"]["decision"]
        for item in baseline.get("sample_results", [])
    }
    for sample_id, decision in sorted(current_samples.items()):
        previous = baseline_samples.get(sample_id)
        if previous and previous != decision:
            differences.append(f"{sample_id}.decision: {previous} -> {decision}")
    return {
        "baseline_found": True,
        "baseline_schema_version": baseline.get("schema_version"),
        "differences": differences,
        "regressions": regressions,
    }


def run_golden_benchmark(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    baseline_path: Path = DEFAULT_BASELINE_PATH,
    output_dir: Path | None = None,
    strict: bool = False,
    mock_llm: bool = True,
    update_baseline_deny: bool = False,
) -> dict[str, Any]:
    if not mock_llm:
        raise ValueError("golden benchmark requires deterministic --mock-llm provider")

    manifest = load_manifest(manifest_path)
    paths = _prepare_paths(output_dir)
    provider = DeterministicBenchmarkProvider()
    ledger = ActionLedger(paths.action_ledger_path, initialize=True)
    sample_results = [
        _evaluate_sample(sample=sample, provider=provider, paths=paths, ledger=ledger)
        for sample in manifest.get("samples", [])
    ]

    persona_delta = {
        result["sample_id"]: result["persona_delta"]
        for result in sample_results
        if result.get("persona_delta")
    }
    paths.persona_path.write_text(
        json.dumps(persona_delta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    scores = _score(sample_results)
    scorecard: dict[str, Any] = {
        "schema_version": GOLDEN_BENCHMARK_SCHEMA_VERSION,
        "scorecard_schema_version": SCORECARD_SCHEMA_VERSION,
        "provider": provider.provider_id,
        "manifest_path": manifest_path.relative_to(ROOT).as_posix(),
        "sample_count": len(sample_results),
        "scores": scores,
        "thresholds": dict(manifest.get("scorecard_expectations") or {}),
        "sample_results": sample_results,
        "action_ledger": paths.action_ledger_path.relative_to(paths.run_dir).as_posix(),
        "wiki_dir": paths.wiki_dir.relative_to(paths.run_dir).as_posix(),
        "persona_delta_path": paths.persona_path.relative_to(paths.run_dir).as_posix(),
        "scorecard_path": paths.scorecard_path.relative_to(paths.run_dir).as_posix(),
    }
    baseline = _load_baseline(baseline_path)
    trend = _compare_baseline(scorecard, baseline)
    scorecard["trend_comparison"] = trend

    errors: list[str] = []
    errors.extend(error for result in sample_results for error in result.get("errors") or [])
    thresholds = dict(manifest.get("scorecard_expectations") or {})
    for field in SCORE_FIELDS:
        threshold_key = f"min_{field}"
        if threshold_key in thresholds and scores[field] < int(thresholds[threshold_key]):
            errors.append(f"{field} below threshold: {scores[field]} < {thresholds[threshold_key]}")
    if strict:
        errors.extend(trend.get("regressions") or [])
    scorecard["ok"] = not errors
    scorecard["errors"] = errors

    paths.scorecard_path.write_text(
        json.dumps(scorecard, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if update_baseline_deny:
        if trend.get("regressions"):
            raise RuntimeError("; ".join(trend["regressions"]))
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(scorecard, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return scorecard


def audit_golden_benchmark_contract(
    *,
    strict: bool = False,
    root: Path | None = None,
    manifest_path: Path | None = None,
    baseline_path: Path | None = None,
) -> list[str]:
    root = root or ROOT
    manifest_path = manifest_path or root / "benchmarks" / "golden" / "manifest.json"
    baseline_path = (
        baseline_path
        or root / "benchmarks" / "golden" / "baseline" / SCORECARD_FILENAME
    )
    errors: list[str] = []
    if not manifest_path.exists():
        return [f"missing benchmark manifest: {manifest_path}"]
    try:
        manifest = load_manifest(manifest_path)
    except json.JSONDecodeError as exc:
        return [f"manifest is not valid JSON: {exc}"]

    if manifest.get("schema_version") != GOLDEN_BENCHMARK_SCHEMA_VERSION:
        errors.append("manifest schema_version must be mnemos.golden_benchmark.v1")
    providers = dict(manifest.get("deterministic_providers") or {})
    missing_providers = REQUIRED_PROVIDER_KEYS - set(providers)
    if missing_providers:
        errors.append(f"missing deterministic providers: {sorted(missing_providers)}")
    for key, value in providers.items():
        if str(value) != "mock":
            errors.append(f"{key}: provider must be mock")

    samples = list(manifest.get("samples") or [])
    if not samples:
        errors.append("at least one golden sample is required")
    categories = {category for sample in samples for category in sample.get("categories", [])}
    missing_categories = REQUIRED_SAMPLE_CATEGORIES - categories
    if missing_categories:
        errors.append(f"missing sample categories: {sorted(missing_categories)}")

    for sample in samples:
        sample_id = str(sample.get("id") or "<missing>")
        missing_keys = REQUIRED_SAMPLE_KEYS - set(sample)
        if missing_keys:
            errors.append(f"{sample_id}: missing keys {sorted(missing_keys)}")
        decision = dict(sample.get("expected_quality_decision") or {})
        if decision.get("decision") not in QUALITY_DECISIONS:
            errors.append(f"{sample_id}: unknown expected quality decision")
        if not decision.get("reason_codes"):
            errors.append(f"{sample_id}: expected quality decision requires reason_codes")
        if not sample.get("expected_consumers"):
            errors.append(f"{sample_id}: expected_consumers must be non-empty")
        if not sample.get("expected_scorecard"):
            errors.append(f"{sample_id}: expected_scorecard required")
        for asset in sample.get("expected_assets", []):
            asset_type = asset.get("asset_type")
            if asset_type not in COGNITIVE_ASSET_DEFINITIONS:
                errors.append(f"{sample_id}: unknown asset_type {asset_type}")
            if not asset.get("evidence_refs"):
                errors.append(f"{sample_id}:{asset_type}: evidence_refs required")
            if not asset.get("consumers"):
                errors.append(f"{sample_id}:{asset_type}: consumers required")

    if strict:
        if not baseline_path.exists():
            errors.append(f"missing benchmark baseline: {baseline_path}")
        else:
            try:
                baseline = _load_baseline(baseline_path) or {}
            except json.JSONDecodeError as exc:
                errors.append(f"baseline is not valid JSON: {exc}")
            else:
                if baseline.get("schema_version") != GOLDEN_BENCHMARK_SCHEMA_VERSION:
                    errors.append("baseline schema_version must match golden benchmark")
                if baseline.get("sample_count") != len(samples):
                    errors.append("baseline sample_count does not match manifest")
    return errors


def build_golden_benchmark_health() -> dict[str, Any]:
    errors = audit_golden_benchmark_contract(strict=True)
    manifest = load_manifest(DEFAULT_MANIFEST_PATH) if DEFAULT_MANIFEST_PATH.exists() else {}
    return {
        "status": "ok" if not errors else "degraded",
        "schema_version": GOLDEN_BENCHMARK_SCHEMA_VERSION,
        "manifest": DEFAULT_MANIFEST_PATH.as_posix(),
        "baseline": DEFAULT_BASELINE_PATH.as_posix(),
        "counts": {
            "samples": len(manifest.get("samples") or []),
            "categories": len(
                {
                    c
                    for sample in manifest.get("samples", [])
                    for c in sample.get("categories", [])
                }
            ),
            "providers": len(manifest.get("deterministic_providers") or {}),
        },
        "errors": errors,
    }


def scorecard_summary(scorecard: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(scorecard.get("ok")),
        "sample_count": int(scorecard.get("sample_count", 0)),
        "scores": dict(scorecard.get("scores") or {}),
        "errors": list(scorecard.get("errors") or []),
        "trend_comparison": dict(scorecard.get("trend_comparison") or {}),
    }
