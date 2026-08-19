"""Canonical source-lineage clustering for Observation calibration.

Calibration counts independent evidence roots, not storage layers.  A Wiki
page derived from one Raw revision therefore joins the same lineage cluster as
that Raw revision and can never provide a second vote merely because it lives
in L2.  The persisted snapshot contains identities and hashes only; visible
source content stays in memory for validator evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Sequence

from core.cognitive.sources import ContentSource, SourceItem


LINEAGE_SNAPSHOT_VERSION = "mnemos.calibration_lineage_snapshot.v1"
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, str) else _canonical_json(value)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_ref(item: SourceItem) -> str:
    if item.raw_revision_id:
        return f"raw-revision:{item.raw_revision_id}"
    return "source:" + _sha256(
        {
            "source_type": item.source_type,
            "file_path": item.file_path,
            "content_hash": item.source_content_hash,
        }
    ).split(":", 1)[1][:32]


def _roots(item: SourceItem) -> tuple[str, ...]:
    roots = tuple(sorted(set(str(value) for value in item.lineage_revision_ids if value)))
    if roots:
        return roots
    if item.source_type == "wiki" and item.content_source == ContentSource.USER_NOTE:
        return ("user-note:" + item.source_content_hash.removeprefix("sha256:"),)
    return ()


def _root_hashes(item: SourceItem, roots: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    values = tuple(sorted(set(item.lineage_root_hashes)))
    if values:
        return values
    if (
        len(roots) == 1
        and item.source_type == "wiki"
        and item.content_source == ContentSource.USER_NOTE
    ):
        return ((roots[0], item.source_content_hash),)
    return ()


@dataclass(frozen=True)
class LineageSourceSnapshot:
    source_ref: str
    source_type: str
    content_source: str
    user_intent: str
    content_hash: str
    lineage_status: str
    lineage_roots: tuple[str, ...]
    lineage_root_hashes: tuple[tuple[str, str], ...]
    source_span_ids: tuple[str, ...]
    cluster_id: str
    independent_eligible: bool

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "source_type": self.source_type,
            "content_source": self.content_source,
            "user_intent": self.user_intent,
            "content_hash": self.content_hash,
            "lineage_status": self.lineage_status,
            "lineage_roots": list(self.lineage_roots),
            "lineage_root_hashes": [list(value) for value in self.lineage_root_hashes],
            "source_span_ids": list(self.source_span_ids),
            "cluster_id": self.cluster_id,
            "independent_eligible": self.independent_eligible,
        }


@dataclass(frozen=True)
class LineageCluster:
    cluster_id: str
    lineage_roots: tuple[str, ...]
    lineage_root_hashes: tuple[tuple[str, str], ...]
    source_refs: tuple[str, ...]
    source_types: tuple[str, ...]
    source_span_ids: tuple[str, ...]
    canonical_text: str
    canonical_text_hash: str
    independent_eligible: bool
    derived_member_count: int

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "lineage_roots": list(self.lineage_roots),
            "lineage_root_hashes": [list(value) for value in self.lineage_root_hashes],
            "source_refs": list(self.source_refs),
            "source_types": list(self.source_types),
            "source_span_ids": list(self.source_span_ids),
            "canonical_text_hash": self.canonical_text_hash,
            "independent_eligible": self.independent_eligible,
            "derived_member_count": self.derived_member_count,
        }


@dataclass(frozen=True)
class CalibrationLineageSnapshot:
    sources: tuple[LineageSourceSnapshot, ...]
    clusters: tuple[LineageCluster, ...]
    snapshot_hash: str
    derived_members_deduplicated: int
    malformed_source_count: int
    unprovenanced_source_count: int

    @property
    def independent_clusters(self) -> tuple[LineageCluster, ...]:
        return tuple(cluster for cluster in self.clusters if cluster.independent_eligible)

    @property
    def source_span_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    span_id
                    for source in self.sources
                    for span_id in source.source_span_ids
                }
            )
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": LINEAGE_SNAPSHOT_VERSION,
            "sources": [source.canonical_payload() for source in self.sources],
            "clusters": [cluster.canonical_payload() for cluster in self.clusters],
            "derived_members_deduplicated": self.derived_members_deduplicated,
            "malformed_source_count": self.malformed_source_count,
            "unprovenanced_source_count": self.unprovenanced_source_count,
        }


def _canonical_cluster_text(items: Sequence[SourceItem]) -> str:
    # A Raw+derived-Wiki cluster uses the Raw representation only.  If the
    # current page contains only a proven derived page, that page represents
    # the lineage root once; it is never counted as an additional source.
    raw_items = [item for item in items if item.source_type == "raw"]
    selected = raw_items or list(items)
    unique: dict[str, str] = {}
    for item in selected:
        unique.setdefault(item.source_content_hash, str(item.content or ""))
    return "\n\n".join(unique[key] for key in sorted(unique))


def build_calibration_lineage(source_items: Sequence[SourceItem]) -> CalibrationLineageSnapshot:
    """Build one independent cluster per exact Raw root.

    A derived page that cites multiple Raw revisions is a shared overlay, not
    proof that the underlying Raw revisions have one origin.  It therefore
    remains visible in the snapshot but cannot merge roots or cast a vote.
    """

    items = tuple(source_items)
    roots_by_index = {index: _roots(item) for index, item in enumerate(items)}

    members: dict[str, list[tuple[int, SourceItem]]] = {}
    for index, item in enumerate(items):
        roots = roots_by_index[index]
        if len(roots) == 1:
            component_key = f"root:{roots[0]}"
        elif len(roots) > 1:
            component_key = "shared-derived:" + _sha256(list(roots))
        else:
            component_key = f"unprovenanced:{index}:{_source_ref(item)}"
        members.setdefault(component_key, []).append((index, item))

    clusters: list[LineageCluster] = []
    cluster_by_item: dict[int, LineageCluster] = {}
    derived_deduplicated = 0
    for component_key, grouped in sorted(members.items()):
        grouped_items = [item for _, item in grouped]
        lineage_roots = tuple(
            sorted({root for index, _ in grouped for root in roots_by_index[index]})
        )
        lineage_root_hashes = tuple(
            sorted(
                {
                    (str(root), str(content_hash))
                    for item in grouped_items
                    for root, content_hash in _root_hashes(item, _roots(item))
                }
            )
        )
        hashes_by_root = {
            root: {content_hash for candidate, content_hash in lineage_root_hashes if candidate == root}
            for root in lineage_roots
        }
        root_hashes_consistent = all(
            len(hashes_by_root.get(root, set())) == 1 for root in lineage_roots
        ) and all(
            bool(_SHA256_PATTERN.fullmatch(content_hash))
            for content_hashes in hashes_by_root.values()
            for content_hash in content_hashes
        )
        independent_eligible = len(lineage_roots) == 1 and all(
            item.lineage_status != "malformed" for item in grouped_items
        ) and root_hashes_consistent
        source_refs = tuple(sorted(_source_ref(item) for item in grouped_items))
        cluster_id = "lineage-cluster:" + _sha256(
            {
                "roots": list(lineage_roots),
                "sources": list(source_refs) if not lineage_roots else [],
            }
        ).split(":", 1)[1][:32]
        text = _canonical_cluster_text(grouped_items)
        derived_count = sum(
            1 for item in grouped_items if item.lineage_status == "derived_exact"
        )
        if not independent_eligible:
            derived_deduplicated += derived_count
        elif any(item.source_type == "raw" for item in grouped_items):
            derived_deduplicated += derived_count
        elif derived_count > 1:
            derived_deduplicated += derived_count - 1
        cluster = LineageCluster(
            cluster_id=cluster_id,
            lineage_roots=lineage_roots,
            lineage_root_hashes=lineage_root_hashes,
            source_refs=source_refs,
            source_types=tuple(sorted({item.source_type for item in grouped_items})),
            source_span_ids=tuple(
                sorted({span for item in grouped_items for span in item.source_span_ids})
            ),
            canonical_text=text,
            canonical_text_hash=_sha256(text),
            independent_eligible=independent_eligible,
            derived_member_count=derived_count,
        )
        clusters.append(cluster)
        for index, _ in grouped:
            cluster_by_item[index] = cluster

    source_snapshots = [
        LineageSourceSnapshot(
            source_ref=_source_ref(item),
            source_type=item.source_type,
            content_source=item.content_source.value,
            user_intent=item.user_intent.value,
            content_hash=item.source_content_hash,
            lineage_status=item.lineage_status or "unprovenanced",
            lineage_roots=roots_by_index[index],
            lineage_root_hashes=_root_hashes(item, roots_by_index[index]),
            source_span_ids=tuple(sorted(set(item.source_span_ids))),
            cluster_id=cluster_by_item[index].cluster_id,
            independent_eligible=cluster_by_item[index].independent_eligible,
        )
        for index, item in enumerate(items)
    ]
    sources = tuple(
        sorted(
            source_snapshots,
            key=lambda source: (
                source.source_ref,
                _canonical_json(source.canonical_payload()),
            ),
        )
    )
    sorted_clusters = tuple(sorted(clusters, key=lambda cluster: cluster.cluster_id))
    malformed_source_count = sum(
        1 for item in items if item.lineage_status == "malformed"
    )
    unprovenanced_source_count = sum(
        1 for roots in roots_by_index.values() if not roots
    )
    snapshot_without_hash = {
        "schema_version": LINEAGE_SNAPSHOT_VERSION,
        "sources": [source.canonical_payload() for source in sources],
        "clusters": [cluster.canonical_payload() for cluster in sorted_clusters],
        "derived_members_deduplicated": derived_deduplicated,
        "malformed_source_count": malformed_source_count,
        "unprovenanced_source_count": unprovenanced_source_count,
    }
    return CalibrationLineageSnapshot(
        sources=sources,
        clusters=sorted_clusters,
        snapshot_hash=_sha256(snapshot_without_hash),
        derived_members_deduplicated=derived_deduplicated,
        malformed_source_count=malformed_source_count,
        unprovenanced_source_count=unprovenanced_source_count,
    )


__all__ = [
    "CalibrationLineageSnapshot",
    "LINEAGE_SNAPSHOT_VERSION",
    "LineageCluster",
    "LineageSourceSnapshot",
    "build_calibration_lineage",
]
