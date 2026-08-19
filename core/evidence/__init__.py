"""
Evidence Layer — 证据图谱

任何 Insight 都可被审计地追溯到源证据：
Insight → Mirror → Observation → Knowledge / Memory

核心模块：
- evidence_graph: 证据节点/边存储与血缘查询
"""

from core.evidence.evidence_graph import (
    EvidenceEdge,
    EvidenceGraph,
    EvidenceNode,
    EvidenceNodeType,
    EvidenceRelationType,
)
from core.evidence.artifact_uri import (
    ALLOWED_ARTIFACT_TYPES,
    ArtifactRef,
    artifact_uri_error,
    build_artifact_ref,
    build_artifact_uri,
    build_content_artifact_uri,
    is_valid_artifact_uri,
)
from core.evidence.artifact_catalog import ArtifactCatalog, ArtifactCatalogEntry

__all__ = [
    "ALLOWED_ARTIFACT_TYPES",
    "ArtifactRef",
    "ArtifactCatalog",
    "ArtifactCatalogEntry",
    "EvidenceNodeType",
    "EvidenceRelationType",
    "EvidenceNode",
    "EvidenceEdge",
    "EvidenceGraph",
    "artifact_uri_error",
    "build_artifact_ref",
    "build_artifact_uri",
    "build_content_artifact_uri",
    "is_valid_artifact_uri",
]
