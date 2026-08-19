"""Value types and pure scoring functions for Wiki metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import logging
import re
from typing import List

from core.utils import LazyPath


logger = logging.getLogger(__name__)
COMPUTE_HEAT_LEVEL_DAYS = 7
COMPUTE_HEAT_LEVEL_DAYS_2 = 30
WIKI_METRICS_CATEGORY_DECAY_DAYS = 7
WIKI_METRICS_CATEGORY_DECAY_DAYS_2 = 30
WIKI_METRICS_DURATION_BUCKET_MONTH_DAYS = 30


class KnowledgeStage(Enum):
    """知识固化阶段（P0-P3，对应项目已有架构）"""

    P0 = "P0"  # Wiki Page: 成熟页面（source_count >= 6, verified）
    P1 = "P1"  # Merged Topic: 已合并（status == 'merged'）
    P2 = "P2"  # Refined: 多次积累（source_count > 1）
    P3 = "P3"  # Raw: 首次创建（source_count <= 1）


class HeatLevel(Enum):
    """简化热力层级"""

    COLD = "cold"  # 30天无更新/访问
    WARM = "warm"  # 7-30天
    HOT = "hot"  # 7天内有更新/访问


class QualityLevel(Enum):
    """质量等级"""

    EXCELLENT = "excellent"  # >= 80分
    GOOD = "good"  # 60-79分
    ACCEPTABLE = "acceptable"  # 40-59分
    POOR = "poor"  # < 40分


DB_PATH = LazyPath("database_dir", "wiki_metrics.db")
WIKI_DIR = LazyPath("wiki_dir")


# ==================== 3. 工具函数 ====================


def _utcnow() -> datetime:
    """返回带时区的当前 UTC 时间"""
    return datetime.now(timezone.utc)


def compute_evidence_level(source_count: int) -> int:
    """根据来源数计算证据等级 (1-4)"""
    if source_count >= 6:
        return 4
    elif source_count >= 4:
        return 3
    elif source_count >= 2:
        return 2
    return 1


def compute_knowledge_stage(source_count: int, status: str = "draft") -> str:
    """计算知识固化阶段 (P0-P3)"""
    if status == "verified" and source_count >= 6:
        return "P0"
    if status == "merged":
        return "P1"
    if source_count > 1:
        return "P2"
    return "P3"


def compute_heat_level(last_updated: str, last_accessed: str | None = None) -> str:
    """根据时间计算热力等级"""
    now = _utcnow()
    try:
        lu = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        days_since_update = (now - lu).days
    except (ValueError, TypeError, AttributeError):
        logger.warning("Unexpected error in wiki_metrics.py", exc_info=True)
        days_since_update = 999

    if last_accessed:
        try:
            la = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
            if la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            days_since_access = (now - la).days
        except (ValueError, TypeError, AttributeError):
            logger.warning("Unexpected error in wiki_metrics.py", exc_info=True)
            days_since_access = 999
        days = min(days_since_update, days_since_access)
    else:
        days = days_since_update

    if days <= COMPUTE_HEAT_LEVEL_DAYS:
        return "hot"
    elif days <= COMPUTE_HEAT_LEVEL_DAYS_2:
        return "warm"
    return "cold"


# --- 中文字段映射辅助函数（修复两张皮） ---


def _heat_level_to_display(level: str) -> str:
    """将内部热力等级映射为用户可见的中文。"""
    return {"hot": "热", "warm": "温", "cold": "冷"}.get(level, level)


def _stage_to_display(stage: str) -> str:
    """将内部知识阶段 P0-P3 映射为用户可见的中文。"""
    return {"P0": "核心", "P1": "成熟", "P2": "发展中", "P3": "原始"}.get(stage, stage)


def _status_to_display(status: str) -> str:
    """将内部状态标识映射为用户可见的中文。"""
    return {
        "draft": "草稿",
        "active": "活跃",
        "review": "待审",
        "pending-verification": "待验证",
        "verified": "已验证",
        "merged": "已合并",
        "deprecated": "废弃",
    }.get(status, status)


def hash_query(query: str) -> str:
    """计算查询的归一化哈希"""
    normalized = re.sub(r"\s+", " ", query.lower().strip())
    return hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def quick_quality_score(content: str) -> float:
    """快速质量评分 (0-100)

    简化的四维度评估：
    - 信息密度：有效字符 / 总长度
    - 结构化：标题、列表、代码块数量
    - 链接质量：内部/外部链接数量
    - 丰富度：字数、实体提及
    """
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) == 3:
            content = parts[2]
    if not content or len(content) < 10:
        return 0.0

    # 1. 密度（去除markdown语法后的实际内容密度）
    clean = re.sub(r"[#*`\[\]\(\)\-_>]", "", content)
    density = min(len(clean.strip()) / max(len(content), 1), 1.0) * 25

    # 2. 结构化（标题、列表、代码块）
    headers = len(re.findall(r"^#{1,6}\s", content, re.MULTILINE))
    lists = len(re.findall(r"^[\s]*[-*+\d]\.", content, re.MULTILINE))
    code_blocks = len(re.findall(r"```", content)) // 2
    structure = min((headers * 3 + lists * 1 + code_blocks * 5) / 20, 1.0) * 25

    # 3. 链接质量
    internal_links = len(re.findall(r"\[\[.*?\]\]", content))
    external_links = len(re.findall(r"\[.*?\]\(https?://", content))
    links = min((internal_links * 3 + external_links * 2) / 10, 1.0) * 25

    # 4. 丰富度
    word_count = len(content.split())
    richness = min(word_count / 500, 1.0) * 25

    return min(density + structure + links + richness, 100.0)


# ==================== 4. 数据类 ====================


@dataclass
class PageMetrics:
    """页面度量数据"""

    wiki_path: str
    title: str = ""
    page_role: str = "knowledge"
    knowledge_stage: str = "P3"
    evidence_level: int = 1
    source_count: int = 0
    source_refs: List[str] = field(default_factory=list)
    heat_level: str = "cold"
    heat_score: float = 0.0
    quality_score: float = 0.0
    quality_level: str = "acceptable"
    completeness: float = 0.0  # 0-1
    freshness_days: int = 999  # 距最后更新天数
    backlink_count: int = 0
    status: str = "draft"
    last_updated: str = ""
    last_accessed: str = ""
    created_at: str = ""
    tags: List[str] = field(default_factory=list)
