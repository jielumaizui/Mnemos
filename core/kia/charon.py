#!/usr/bin/env python3
# Charon — 冥河渡夫 — 连接 Worker，摆渡数据于各系统之间
# 原模块: connect_worker.py

import logging

"""Connect Worker - L2 → L3 关联层：抽取实体、维护关系、分类 Inbox 页面。"""

import re
import json
import argparse
import sys
import hashlib
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.kia.policy import get_effective_policy
from core.cognitive.state_contract import sha256_json
from core.kia.charon_entities import (  # noqa: F401
    CHINESE_SURNAMES,
    CONCEPT_KEYWORDS,
    EntityExtractor,
    PROJECT_INDICATOR_PATTERN,
    PROJECT_INDICATORS,
    TECH_KEYWORDS,
)
from core.kia.charon_page_mutation import render_classified_page
from core.cli.periodic import add_periodic_loop_args, resolve_max_cycles, run_periodic_loop
from core.utils import LazyPath
from core.frontmatter import parse_frontmatter, fm_get
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    authorize_exact_markdown_action,
    submit_or_write_markdown_with_decision,
)
from core.trust.markdown_update import trusted_markdown_move
from core.trust.models import sha256_text
from core.vaults.naming import is_source_prefixed_stem, safe_display_slug

# Constants extracted from magic numbers
RELATION_ENGINE_DURATION_BUCKET_MONTH_DAYS = 30
AGE_DAYS = 86400
MAIN_SLEEP = 600
logger = logging.getLogger(__name__)

CHARON_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:charon-page-classification",
    contract_revision_id="mnemos.charon_page_classification.v1",
    contract_text=(
        "Charon may update or move only the exact page preimage selected by its "
        "deterministic classification result, or mark that exact page for review."
    ),
    source_namespace="charon-page-classification",
    producer="charon-auto-classify",
    producer_code_hash=sha256_json(
        {
            "module": "core.kia.charon",
            "producers": ["_move_page_to_category", "_mark_needs_review"],
            "version": "mnemos.charon_page_classification.v1",
        }
    ),
    evaluator_id="charon-page-classification-evaluator",
    constraints=(
        "Source, destination, page preimage, classification, and output bytes remain exact.",
        "Basename collisions and dry-run results may not execute a formal mutation.",
    ),
    approved_candidate_key="apply_exact_charon_classification",
    approved_candidate_summary="Apply the exact deterministic page classification.",
    rejected_candidate_key="retain_page_for_manual_classification",
    rejected_candidate_summary="Retain the page when classification or bytes drift.",
    approved_reason_code="charon_classification_binding_verified",
    rejected_reason_code="charon_classification_binding_rejected",
    committed_metric="charon_classification_committed",
    rejected_metric="unbound_charon_classification_count",
)


# 模块级路径常量：首次访问时才解析（避免 import 时触发 get_config 副作用）
WIKI_DIR = LazyPath("wiki_dir")
INBOX_DIR = LazyPath("wiki_dir", "00-Inbox")
PEOPLE_DIR = LazyPath("wiki_dir", "01-People")
PROJECTS_DIR = LazyPath("wiki_dir", "02-Projects")
TECH_DIR = LazyPath("wiki_dir", "03-Tech")
CONCEPTS_DIR = LazyPath("wiki_dir", "04-Concepts")
MOCS_DIR = LazyPath("wiki_dir", "05-MOCs")
RETROS_DIR = LazyPath("wiki_dir", "06-Retrospectives")
SHADOW_DIR = LazyPath("wiki_dir", "07-Shadow")
REPORTS_DIR = LazyPath("wiki_dir", "99-Reports")
FLYWHEEL_DIR = LazyPath("wiki_dir", "06-Retrospectives", "flywheel")
ENTROPY_DIR = LazyPath("wiki_dir", "06-Retrospectives", "entropy")
REMINDERS_DIR = LazyPath("wiki_dir", "08-Reminders")

ALL_DIRS = [
    INBOX_DIR,
    PEOPLE_DIR,
    PROJECTS_DIR,
    TECH_DIR,
    CONCEPTS_DIR,
    MOCS_DIR,
    RETROS_DIR,
    SHADOW_DIR,
    REPORTS_DIR,
    FLYWHEEL_DIR,
    ENTROPY_DIR,
    REMINDERS_DIR,
]

# 知识图谱内部数据目录（相对于 wiki_base）
_KG_SUBDIR = ".kg"


def _safe_filename(name: str) -> str:
    """生成安全的文件名（保留可读性）"""
    # 替换不安全的字符，但保留中文
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    # 限制长度
    if len(safe) > 60:
        hash_suffix = hashlib.md5(safe.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
        safe = f"{safe[:60]}_{hash_suffix}"
    return safe or "untitled"


def _ensure_dirs():
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# ========== 关系引擎 ==========


class RelationEngine:
    """关系分析和建立"""

    def __init__(self, half_life_days: int | None = None, db_path: str | Path | None = None):
        if half_life_days is None:
            half_life_days = get_effective_policy().get(
                "knowledge_graph.freshness_decay_half_life_days",
                RELATION_ENGINE_DURATION_BUCKET_MONTH_DAYS,
            )
        self.half_life_days = half_life_days
        self.decay_lambda = math.log(2) / max(half_life_days, 1)
        self.co_occurrence: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.entity_docs: Dict[str, List[str]] = defaultdict(list)  # entity -> [doc_paths]
        self.db_path = Path(db_path).expanduser() if db_path else None
        if self.db_path:
            self._init_db()

    def analyze_session(
        self, doc_path: str, entities: Dict[str, Set[str]], timestamp: datetime | None = None
    ):
        """分析单个 session 中的所有实体共现"""
        timestamp = timestamp or datetime.now()
        age_days = max((datetime.now() - timestamp).total_seconds() / AGE_DAYS, 0)
        weight = round(math.exp(-self.decay_lambda * age_days), 4)

        all_entities = set()
        for category, items in entities.items():
            all_entities.update(items)

        self.entity_docs[doc_path].extend(all_entities)

        # 共现统计
        entities_list = list(all_entities)
        for i, e1 in enumerate(entities_list):
            for e2 in entities_list[i + 1 :]:
                if e1 != e2:
                    self.co_occurrence[e1][e2] += weight
                    self.co_occurrence[e2][e1] += weight
                    self._persist_relation(e1, e2, weight, timestamp)

    def get_relations(self, entity: str, min_count: float = 1.0) -> List[Tuple[str, float]]:
        """获取实体的关系列表"""
        relations = self.co_occurrence.get(entity, {})  # type: ignore[var-annotated]
        return sorted(
            [(e, round(c, 3)) for e, c in relations.items() if c >= min_count],
            key=lambda x: x[1],
            reverse=True,
        )

    def decrement(self, e1: str, e2: str, amount: float = 1.0):
        """Decrease a stored co-occurrence weight for rollback or negative feedback."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        if amount == 0:
            return
        self.co_occurrence[e1][e2] = max(0.0, self.co_occurrence[e1][e2] - amount)
        self.co_occurrence[e2][e1] = max(0.0, self.co_occurrence[e2][e1] - amount)
        if self.db_path:
            self._decrement_persisted_relation(e1, e2, amount)

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS co_occurrence_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_a TEXT NOT NULL,
                    entity_b TEXT NOT NULL,
                    co_occurrence_count INTEGER DEFAULT 0,
                    weight REAL DEFAULT 0,
                    session_count INTEGER DEFAULT 0,
                    first_seen TEXT,
                    last_seen TEXT,
                    UNIQUE(entity_a, entity_b)
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(co_occurrence_relations)")}
            if "weight" not in columns:
                conn.execute("ALTER TABLE co_occurrence_relations ADD COLUMN weight REAL DEFAULT 0")
            if "session_count" not in columns:
                conn.execute(
                    "ALTER TABLE co_occurrence_relations ADD COLUMN session_count INTEGER DEFAULT 0"
                )
            if "first_seen" not in columns:
                conn.execute("ALTER TABLE co_occurrence_relations ADD COLUMN first_seen TEXT")
            if "last_seen" not in columns:
                conn.execute("ALTER TABLE co_occurrence_relations ADD COLUMN last_seen TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_coocc_entity_a ON co_occurrence_relations(entity_a)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_coocc_entity_b ON co_occurrence_relations(entity_b)"
            )

    def _persist_relation(self, e1: str, e2: str, weight: float, timestamp: datetime):
        if not self.db_path:
            return
        entity_a, entity_b = sorted([e1, e2])
        now = timestamp.isoformat()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                INSERT INTO co_occurrence_relations
                    (
                        entity_a, entity_b, co_occurrence_count, weight,
                        session_count, first_seen, last_seen
                    )
                VALUES (?, ?, 1, ?, 1, ?, ?)
                ON CONFLICT(entity_a, entity_b) DO UPDATE SET
                    co_occurrence_count = co_occurrence_count + 1,
                    weight = weight + excluded.weight,
                    session_count = session_count + 1,
                    last_seen = excluded.last_seen
            """,
                (entity_a, entity_b, weight, now, now),
            )

    def _decrement_persisted_relation(self, e1: str, e2: str, amount: float):
        entity_a, entity_b = sorted([e1, e2])
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                UPDATE co_occurrence_relations
                SET weight = MAX(weight - ?, 0),
                    co_occurrence_count = MAX(co_occurrence_count - 1, 0)
                WHERE entity_a=? AND entity_b=?
            """,
                (amount, entity_a, entity_b),
            )


def _flatten_entities(entities: Dict[str, Set[str]]) -> Set[str]:
    all_entities = set()
    for items in entities.values():
        all_entities.update(items)
    return all_entities


def _extract_page_timestamp(page_path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(page_path.stat().st_mtime)
    except OSError:
        return datetime.now()


class ConnectModule:
    """连接 Worker 的轻量热插拔封装。"""

    def __init__(self, wiki_base: str | Path | None = None, db_path: str | Path | None = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else Path(str(WIKI_DIR))
        self.db_path = (
            Path(db_path).expanduser()
            if db_path
            else self.wiki_base / _KG_SUBDIR / "knowledge_graph.db"
        )
        self.extractor = EntityExtractor(wiki_base=self.wiki_base)

    def handle_event(self, event_type: str, data: Dict):
        if event_type in {"page.created", "page.modified", "distill_complete"}:
            page_path = data.get("page_path")
            if page_path:
                return self._incremental_process(Path(page_path))
        if (
            event_type == "scheduler.hourly"
            and data.get("task_name") == "connect_consistency_check"
        ):
            return run_connect_cycle()
        return None

    def _incremental_process(self, page_path: Path) -> Dict:
        if not page_path.exists():
            return {"status": "missing", "page_path": str(page_path)}

        text = page_path.read_text(encoding="utf-8")
        cwd = ""
        match = re.search(r"working_dir:\s*`?([^`\n]+)", text)
        if match:
            cwd = match.group(1).strip()

        old_entities = self._get_stored_entities(page_path)
        new_entities_by_type = self.extractor.extract(text, cwd=cwd)
        new_entities = _flatten_entities(new_entities_by_type)
        removed = old_entities - new_entities
        added = new_entities - old_entities

        # NOTE: ConnectModule 不再启动旧 RelationEngine；
        # 关系分析和持久化统一由 KnowledgeGraph / EntityManager 承担。
        self._store_entities(page_path, new_entities)

        # 同步写入 EntityManager，确保 knowledge_graph.db/entities 表有记录
        try:
            from core.kia.entity_manager import EntityManager

            em = EntityManager(db_path=self.db_path)
            for entity_name in new_entities:
                em.add_entity(name=entity_name, entity_type="concept", wiki_page=str(page_path))
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("[Connect] 增量实体写入 EntityManager 失败", exc_info=True)

        return {
            "status": "ok",
            "page_path": str(page_path),
            "added": sorted(added),
            "removed": sorted(removed),
        }

    def _get_stored_entities(self, page_path: Path) -> Set[str]:
        marker = self._marker_path(page_path)
        if not marker.exists():
            return set()
        try:
            return set(json.loads(marker.read_text(encoding="utf-8")).get("entities", []))
        except (json.JSONDecodeError, ValueError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at charon.py", exc_info=True
            )
            return set()

    def _store_entities(self, page_path: Path, entities: Set[str]):
        marker = self._marker_path(page_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        # trusted-scan: system_state owner=kia target=kg_entity_marker expires=never not a Wiki page mutation
        marker.write_text(
            json.dumps(
                {"page_path": str(page_path), "entities": sorted(entities)}, ensure_ascii=False
            ),
            encoding="utf-8",
        )

    def _marker_path(self, page_path: Path) -> Path:
        return (
            self.wiki_base
            / _KG_SUBDIR
            / "connect_entities"
            / f"{hashlib.md5(str(page_path).encode(), usedforsecurity=False).hexdigest()}.json"
        )


# ========== 文件夹分类规范（Taxonomy）==========

# 显式 frontmatter 类型 -> 内部分类名
_TYPE_TO_CATEGORY: Dict[str, str] = {
    # 技术
    "technology": "tech",
    "tech": "tech",
    "snippet": "tech",
    "dataset": "tech",
    "data-insight": "tech",
    # 概念
    "concept": "concepts",
    # 项目/决策
    "project": "projects",
    "decision": "projects",
    # 人物
    "people": "people",
    "person": "people",
    # 复盘
    "retrospective": "retrospective",
    "review": "retrospective",
    "retro": "retrospective",
    # 枢纽
    "moc": "mocs",
    # 报告
    "report": "reports",
    "book": "reports",
    "strategy": "reports",
}

# 一级目录映射
_CATEGORY_TO_DIR: Dict[str, LazyPath] = {
    "projects": PROJECTS_DIR,
    "tech": TECH_DIR,
    "people": PEOPLE_DIR,
    "concepts": CONCEPTS_DIR,
    "retrospective": RETROS_DIR,
    "mocs": MOCS_DIR,
    "reports": REPORTS_DIR,
}

# 二级主题映射：关键词（小写） -> 子文件夹名
TECH_TOPIC_FOLDERS: Dict[str, str] = {
    "codex": "codex",
    "opencode": "opencode",
    "百度千帆": "百度千帆",
    "千帆": "百度千帆",
    "openclaw": "openclaw",
    "hermes": "hermes",
    "memos": "memos",
    "mnemos": "mnemos",
    "ragflow": "ragflow",
    "scrapling": "scrapling",
    "cc-switch": "cc-switch",
    "ccswitch": "cc-switch",
    "ollama": "ollama",
    "git": "git",
    "github": "git",
    "python": "python",
    "windows": "windows",
    "wsl": "windows",
    "macos": "macos",
    "linux": "linux",
    "docker": "docker",
    "orbstack": "docker",
    "ppt": "ppt",
    "pptx": "ppt",
    "cron": "cron",
    "ssh": "ssh",
    "homebrew": "homebrew",
    "kimi": "kimi",
    "openai": "openai",
    "fastapi": "fastapi",
    "redis": "redis",
    "react": "react",
    "vue": "vue",
    "django": "django",
    "flask": "flask",
    "kia": "mnemos",
    "charon": "mnemos",
    "蒸馏": "mnemos",
    "frontmatter": "mnemos",
}

CONCEPT_TOPIC_FOLDERS: Dict[str, str] = {
    "静态分析": "工程实践",
    "数据口径": "数据",
    "认知伙伴": "知识管理",
    "关系账本": "关系",
    "复盘": "复盘",
    "决策": "决策",
    "kpi": "管理",
    "okr": "管理",
    "画像": "管理",
    "知识图谱": "知识管理",
    "wiki": "知识管理",
    "漏斗": "产品",
    "mvp": "产品",
    "growth": "产品",
    "数据治理": "数据",
    "etl": "数据",
    "olap": "数据",
}

RETROSPECTIVE_TOPIC_FOLDERS: Dict[str, str] = {
    "flywheel": "flywheel",
    "飞轮": "flywheel",
    "entropy": "entropy",
    "熵减": "entropy",
    "复盘": "复盘",
}

FORMAL_PAGE_DIRS = (
    PEOPLE_DIR,
    PROJECTS_DIR,
    TECH_DIR,
    CONCEPTS_DIR,
    RETROS_DIR,
)


def _safe_subfolder(name: str) -> str:
    """生成安全的子目录名（保留中文）。"""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip()
    return safe or "其他"


_INVALID_PROJECT_NAMES = {
    "---",
    "auto_classified",
    "classified_at",
    "category",
    "subfolder",
    "名称",
    "类型",
    "状态",
    "dna_type",
    "dna_domain",
    "project",
    "import",
    "from",
    "class",
    "def",
    "return",
    "if",
    "for",
    "in",
    "with",
}


def _is_valid_project_name(name: str) -> bool:
    """过滤掉从 frontmatter 中误提取的项目名。"""
    if not name or len(name.strip()) < 2:
        return False
    if name.strip().lower() in _INVALID_PROJECT_NAMES:
        return False
    if name.startswith("-") or name.startswith("_"):
        return False
    return True


def _normalize_match_text(text: str) -> str:
    return text.lower().strip()


def _first_topic_match(text: str, tags: Set[str], mapping: Dict[str, str]) -> Optional[str]:
    """先在标题中匹配，再在 tags 中匹配。标题通常更反映主题。"""
    text_norm = _normalize_match_text(text)
    for key, folder in mapping.items():
        if _normalize_match_text(key) in text_norm:
            return folder
    for tag in tags:
        key = _normalize_match_text(tag)
        if key in mapping:
            return mapping[key]
    return None


def _extract_first_heading(page_path: Path) -> Optional[str]:
    """从页面正文提取第一个一级标题，作为 frontmatter 名称的兜底。"""
    try:
        text = page_path.read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    except (OSError, IOError):
        pass
    return None


def _collect_tags(fm: Dict[str, Any]) -> Set[str]:
    """从 frontmatter 收集可用于主题匹配的关键词集合。"""
    tags: Set[str] = set()
    keywords = fm_get(fm, "keywords")
    if isinstance(keywords, list):
        tags.update(str(k) for k in keywords)
    elif isinstance(keywords, dict):
        for v in keywords.values():
            if isinstance(v, list):
                tags.update(str(k) for k in v)
    explicit_tags = fm.get("tags") or fm.get("标签")
    if isinstance(explicit_tags, list):
        tags.update(str(k) for k in explicit_tags)
    return tags


# ========== 自动分类与移动 ==========


def _classify_page(entities: Dict[str, Set[str]]) -> Optional[str]:
    """基于实体数量决定页面分类，返回类别名或 None（无法分类）"""
    scores = {
        "projects": len(entities.get("projects", set())),
        "tech": len(entities.get("tech", set())),
        "people": len(entities.get("people", set())),
        "concepts": len(entities.get("concepts", set())),
    }
    if not any(scores.values()):
        return None
    # 取数量最多的类别；平局时按 projects > tech > people > concepts 优先级
    best = max(
        scores, key=lambda k: (scores[k], {"projects": 4, "tech": 3, "people": 2, "concepts": 1}[k])
    )
    return best if scores[best] > 0 else None


def resolve_page_folder(
    page_path: Path,
    frontmatter: Optional[Dict[str, Any]] = None,
    entities: Optional[Dict[str, Set[str]]] = None,
) -> Optional[Path]:
    """
    根据 frontmatter 类型、标题关键词、tags 和实体计数决定目标目录。

    Returns:
        目标目录的绝对 Path（基于 wiki_dir），无法分类时返回 None。
    """
    fm = frontmatter or {}
    title = fm_get(fm, "name")
    if not title or title == page_path.stem:
        title = _extract_first_heading(page_path) or page_path.stem
    tags = _collect_tags(fm)

    # 1. 先看显式类型
    page_type = str(fm_get(fm, "type") or "").strip().lower()
    category = _TYPE_TO_CATEGORY.get(page_type)

    # 2. 标题中的强提示词兜底
    if not category:
        title_norm = _normalize_match_text(title)
        if any(_normalize_match_text(k) in title_norm for k in RETROSPECTIVE_TOPIC_FOLDERS):
            category = "retrospective"
        elif any(_normalize_match_text(k) in title_norm for k in CONCEPT_TOPIC_FOLDERS):
            category = "concepts"
        elif any(_normalize_match_text(k) in title_norm for k in TECH_TOPIC_FOLDERS):
            category = "tech"

    # 3. 再按实体计数兜底
    if not category and entities:
        category = _classify_page(entities)

    if not category:
        return None

    base_dir_lazy = _CATEGORY_TO_DIR.get(category)
    if base_dir_lazy is None:
        return None
    target_dir = Path(str(base_dir_lazy))

    # 3. 二级子目录
    subfolder: Optional[str] = None
    if category == "tech":
        subfolder = _first_topic_match(title, tags, TECH_TOPIC_FOLDERS)
    elif category == "concepts":
        subfolder = _first_topic_match(title, tags, CONCEPT_TOPIC_FOLDERS)
    elif category == "retrospective":
        subfolder = _first_topic_match(title, tags, RETROSPECTIVE_TOPIC_FOLDERS)
        if not subfolder:
            subfolder = "复盘"
    elif category == "projects":
        projects: List[str] = []
        if entities:
            projects.extend(entities.get("projects", set()))
        proj_fm = fm_get(fm, "project")
        if isinstance(proj_fm, str):
            projects.append(proj_fm)
        elif isinstance(proj_fm, list):
            projects.extend(str(p) for p in proj_fm)
        projects = [p for p in projects if _is_valid_project_name(p)]
        if projects:
            subfolder = _safe_subfolder(sorted(projects, key=len, reverse=True)[0])
        # 若项目名无效/缺失，按标题回退到 tech/concept，避免把技术报告塞进 02-Projects 根目录
        if not projects or subfolder == "其他":
            title_norm = _normalize_match_text(title)
            if any(_normalize_match_text(k) in title_norm for k in CONCEPT_TOPIC_FOLDERS):
                category = "concepts"
                target_dir = Path(str(_CATEGORY_TO_DIR[category]))
                subfolder = _first_topic_match(title, tags, CONCEPT_TOPIC_FOLDERS)
            elif any(_normalize_match_text(k) in title_norm for k in TECH_TOPIC_FOLDERS):
                category = "tech"
                target_dir = Path(str(_CATEGORY_TO_DIR[category]))
                subfolder = _first_topic_match(title, tags, TECH_TOPIC_FOLDERS)
    elif category == "people":
        people: List[str] = []
        if entities:
            people.extend(entities.get("people", set()))
        if people:
            subfolder = _safe_subfolder(sorted(people, key=len, reverse=True)[0])

    if subfolder:
        target_dir = target_dir / _safe_subfolder(subfolder)

    return target_dir


def _classified_page_text(page_path: Path, target_dir: Path) -> tuple[str, str] | None:
    """Render classification frontmatter without mutating the formal page."""
    try:
        text = page_path.read_text(encoding="utf-8")
        return text, render_classified_page(text, target_dir, Path(str(WIKI_DIR)))
    except (OSError, ValueError, TypeError, ImportError, RuntimeError):
        logger.warning("生成 frontmatter 分类失败: %s", page_path, exc_info=True)
        return None


def _display_stem_for_page(page_path: Path) -> str:
    """Return the canonical display-title stem for a Markdown page."""
    try:
        text = page_path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
    except (OSError, IOError):
        fm = None
    title = fm_get(fm, "name")
    if not title or title == page_path.stem:
        title = _extract_first_heading(page_path) or page_path.stem
    return safe_display_slug(title)


def _target_filename_for_page(page_path: Path, target_dir: Path) -> str:
    """Return the filename Charon should use when placing a page."""
    try:
        rel = page_path.relative_to(Path(str(WIKI_DIR)))
        first_part = rel.parts[0] if rel.parts else ""
    except (ValueError, RuntimeError):
        first_part = ""

    if first_part == "00-Inbox" or is_source_prefixed_stem(page_path.stem):
        return f"{_display_stem_for_page(page_path)}{page_path.suffix}"
    return page_path.name


def _move_page_to_category(page_path: Path, target_dir: Path, dry_run: bool = False) -> Dict:
    """将页面移动到解析出的目标目录（支持二级子目录）"""
    result: Dict[str, Any] = {"status": "skipped", "from": str(page_path), "to": None}

    target_dir = Path(str(target_dir))
    target_dir.mkdir(parents=True, exist_ok=True)

    target_path = target_dir / _target_filename_for_page(page_path, target_dir)
    collisions = find_formal_basename_collisions(page_path, target_path.stem)
    if collisions and target_path != page_path:
        result["status"] = "duplicate_basename"
        result["existing"] = [str(path) for path in collisions]
        result["to"] = str(target_path)
        return result

    # 避免覆盖：若目标已存在则追加序号
    if target_path.exists() and target_path != page_path:
        stem = target_path.stem
        suffix = target_path.suffix
        for i in range(1, 1000):
            candidate = target_dir / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                target_path = candidate
                break
        else:
            result["status"] = "collision"
            return result

    if dry_run:
        result["status"] = "dry_run"
        result["to"] = str(target_path)
        return result

    try:
        classified = _classified_page_text(page_path, target_dir)
        if classified is None:
            result["status"] = "error"
            result["error"] = "failed to render classification frontmatter"
            return result
        source_content, classified_text = classified
        wiki_base = Path(str(WIKI_DIR))
        evidence_refs = [f"page:{page_path.name}"]
        decision_facts = {
            "schema_version": "mnemos.charon_classification_facts.v1",
            "source_path": str(page_path.resolve(strict=False)),
            "target_path": str(target_path.resolve(strict=False)),
            "target_directory": str(target_dir.resolve(strict=False)),
            "operation": "update" if target_path == page_path else "move",
        }
        # 如果目标就是当前位置（已在正确目录），不移动
        if target_path == page_path:
            receipt = submit_or_write_markdown_with_decision(
                decision_policy=CHARON_MARKDOWN_POLICY,
                decision_facts=decision_facts,
                decision_task=f"Update classification for {page_path.name}",
                decision_goal="Apply the exact deterministic classification metadata.",
                decision_created_at=datetime.now(timezone.utc).isoformat(),
                wiki_base=wiki_base,
                target_path=page_path,
                content=classified_text,
                source="charon_auto_classify",
                actor="system",
                evidence_refs=evidence_refs,
                proposed_action="update_page_classification",
                expected_existing_hash=sha256_text(source_content),
            )
            result["status"] = "proposed" if receipt.intercepted else "already_there"
            result["proposal_id"] = receipt.proposal_id
            result["to"] = str(target_path)
            return result
        proposed_action = "move_classified_page"
        material_action = authorize_exact_markdown_action(
            policy=CHARON_MARKDOWN_POLICY,
            wiki_base=wiki_base,
            target_path=target_path,
            content=classified_text,
            proposed_action=proposed_action,
            expected_existing_hash=None,
            source_facts=decision_facts,
            evidence_refs=evidence_refs,
            task=f"Move classified page {page_path.name}",
            goal="Move the exact page preimage to its deterministic category.",
            created_at=datetime.now(timezone.utc).isoformat(),
            source_path=page_path,
            source_content_hash=sha256_text(source_content),
        )
        receipt = trusted_markdown_move(
            wiki_base,
            page_path,
            target_path,
            classified_text,
            source_content,
            "charon_auto_classify",
            proposed_action,
            evidence_refs=evidence_refs,
            material_action=material_action,
        )
        result["status"] = "proposed" if receipt.intercepted else "moved"
        result["proposal_id"] = receipt.proposal_id
        result["to"] = str(target_path)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
        result["status"] = "error"
        result["error"] = str(e)
    return result


def find_formal_basename_collisions(page_path: Path, basename: str | None = None) -> List[Path]:
    """Return formal-vault pages that share the incoming canonical basename."""
    page_path = Path(page_path)
    stem = basename or _display_stem_for_page(page_path)
    try:
        source_resolved = page_path.resolve()
    except OSError:
        source_resolved = page_path

    collisions: List[Path] = []
    for root_lazy in FORMAL_PAGE_DIRS:
        root = Path(str(root_lazy))
        if not root.exists():
            continue
        for candidate in root.rglob(f"{stem}.md"):
            try:
                candidate_resolved = candidate.resolve()
            except OSError:
                candidate_resolved = candidate
            if candidate_resolved == source_resolved:
                continue
            collisions.append(candidate)
    return sorted(collisions)


# ========== 主流程 ==========


_MAX_ENTITIES_PER_DOC = 15
_MAX_CO_OCCURS_PER_DOC = 25


def _init_connect_state() -> tuple[
    Dict[str, Dict[str, Set[str]]],
    Dict[Path, Dict[str, Set[str]]],
    Dict[str, Set[str]],
]:
    all_entities: Dict[str, Dict[str, Set[str]]] = {
        "people": defaultdict(set),
        "projects": defaultdict(set),
        "tech": defaultdict(set),
        "concepts": defaultdict(set),
    }
    file_entities: Dict[Path, Dict[str, Set[str]]] = {}
    project_tech: Dict[str, Set[str]] = defaultdict(set)
    return all_entities, file_entities, project_tech


def _extract_cwd_from_text(text: str) -> str:
    m = re.search(r"working_dir:\s*`?([^`\n]+)", text)
    return m.group(1).strip() if m else ""


def _write_cooccurrence_relations(
    md_file: Path,
    entities: Dict[str, Set[str]],
    doc_name: str,
) -> None:
    try:
        from core.kia.knowledge_graph import KnowledgeGraph
        from core.kia.relation_schema import Relation, RelationType, RelationEvidence
        from core.kia.entity_manager import EntityManager
    except ImportError:
        logger.warning("KnowledgeGraph 模块导入失败")
        return

    try:
        kg = KnowledgeGraph()
        em = EntityManager()
        all_entities_in_doc = set()
        for category_items in entities.values():
            all_entities_in_doc.update(category_items)

        if len(all_entities_in_doc) > _MAX_ENTITIES_PER_DOC:
            priority = set(entities.get("projects", [])) | set(entities.get("tech", []))
            others = sorted(all_entities_in_doc - priority)
            keep_count = max(0, _MAX_ENTITIES_PER_DOC - len(priority))
            all_entities_in_doc = priority | set(others[:keep_count])

        entities_list = list(all_entities_in_doc)
        for entity_name in entities_list:
            em.add_entity(name=entity_name, entity_type="concept", wiki_page=str(md_file))
        _extract_page_timestamp(md_file)

        pairs = _build_cooccurrence_pairs(entities_list)
        for e1, e2 in pairs:
            kg.add_relation(
                Relation(
                    source=e1,
                    target=e2,
                    relation_type=RelationType.CO_OCCURS,
                    strength=0.5,
                    confidence=0.5,
                    source_method="connect_worker",
                    evidence=[
                        RelationEvidence(
                            evidence_type="co_occurrence",
                            content=f"共现于 {doc_name}",
                        )
                    ],
                )
            )
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
        logger.warning("KnowledgeGraph 导入/写入失败: %s", doc_name)


def _build_cooccurrence_pairs(entities_list: List[str]) -> List[Tuple[str, str]]:
    pairs = []
    for i, e1 in enumerate(entities_list):
        for e2 in entities_list[i + 1 :]:
            if e1 != e2:
                pairs.append((e1, e2))
                if len(pairs) >= _MAX_CO_OCCURS_PER_DOC:
                    return pairs
    return pairs


def _aggregate_entities(
    entities: Dict[str, Set[str]],
    doc_name: str,
    all_entities: Dict[str, Dict[str, Set[str]]],
    project_tech: Dict[str, Set[str]],
) -> None:
    for category, items in entities.items():
        for item in items:
            all_entities[category][item].add(doc_name)
    for proj in entities.get("projects", set()):
        project_tech[proj].update(entities.get("tech", set()))


def _process_inbox_file(
    md_file: Path,
    extractor: EntityExtractor,
    all_entities: Dict[str, Dict[str, Set[str]]],
    file_entities: Dict[Path, Dict[str, Set[str]]],
    project_tech: Dict[str, Set[str]],
    dry_run: bool = False,
    write_relations: bool = True,
) -> None:
    text = md_file.read_text(encoding="utf-8")
    doc_name = md_file.stem[:40]
    cwd = _extract_cwd_from_text(text)
    entities = extractor.extract(text, cwd=cwd)
    file_entities[md_file] = entities

    if not dry_run and write_relations:
        _write_cooccurrence_relations(md_file, entities, doc_name)
    _aggregate_entities(entities, doc_name, all_entities, project_tech)


def _preview_dry_run(
    file_entities: Dict[Path, Dict[str, Set[str]]],
    all_entities: Dict[str, Dict[str, Set[str]]],
) -> None:
    total = sum(len(v) for v in all_entities.values())
    logger.info("[Connect] [DRY RUN] 将生成 %s 个实体节点 + MOC 枢纽", total)
    for md_file, entities in file_entities.items():
        try:
            fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
        except (OSError, IOError):
            fm = None
        target_dir = resolve_page_folder(md_file, fm, entities)
        if target_dir is not None:
            rel_target = target_dir.relative_to(Path(str(WIKI_DIR)))
            logger.info("[Connect] [DRY RUN] 将移动 %s -> %s/", md_file.name, rel_target.as_posix())
        else:
            logger.info("[Connect] [DRY RUN] %s 无法分类，留在 Inbox", md_file.name)


def _mark_needs_review(md_file: Path) -> None:
    try:
        import yaml
        text = md_file.read_text(encoding="utf-8")
        wiki_base = Path(str(WIKI_DIR))
        now = datetime.now().isoformat(timespec="seconds")
        review_fm = {
            "needs_review": True,
            "review_reason": "auto_classify_failed",
            "review_at": now,
        }
        evidence_refs = [f"page:{md_file.name}", "classification:auto_classify_failed"]

        def submit_review_marker(new_text: str) -> None:
            """Submit the exact failed-classification marker through trust."""

            submit_or_write_markdown_with_decision(
                decision_policy=CHARON_MARKDOWN_POLICY,
                decision_facts={
                    "schema_version": "mnemos.charon_review_marker_facts.v1",
                    "page_path": str(md_file.resolve(strict=False)),
                    "review_fields": review_fm,
                },
                decision_task=f"Mark {md_file.name} for classification review",
                decision_goal="Expose the exact deterministic-classification failure.",
                decision_created_at=datetime.now(timezone.utc).isoformat(),
                wiki_base=wiki_base,
                target_path=md_file,
                content=new_text,
                source="charon_auto_classify",
                actor="system",
                evidence_refs=evidence_refs,
                proposed_action="mark_needs_review",
                expected_existing_hash=sha256_text(text),
            )

        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1]) or {}
                fm.update(review_fm)
                new_fm = yaml.dump(fm, allow_unicode=True, sort_keys=False)
                new_text = f"---\n{new_fm}---{parts[2]}"
                submit_review_marker(new_text)
                return
        fm_yaml = yaml.dump(review_fm, allow_unicode=True, sort_keys=False)
        submit_review_marker(f"---\n{fm_yaml}---\n{text}")
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
        logger.warning("标记 needs_review 失败: %s", md_file, exc_info=True)


def _is_malformed_or_session_stem(stem: str) -> bool:
    """判断文件名是否是旧版 session__ 产物或格式异常，应避免自动分类移动。"""
    if stem.startswith("session__"):
        return True
    # 文件名中包含路径分隔符或以连字符开头，通常来自错误解析的链接/占位页
    if "/" in stem or "\\" in stem or stem.startswith("-"):
        return True
    return False


def _handle_classified_file(
    md_file: Path,
    entities: Dict[str, Set[str]],
    dry_run: bool,
    stats: Dict[str, int],
) -> None:
    if _is_malformed_or_session_stem(md_file.stem):
        logger.info("[Connect] %s 是会话原始文件或文件名异常，留在 Inbox", md_file.name)
        stats["review"] += 1
        return

    try:
        fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
    except (OSError, IOError):
        fm = None

    target_dir = resolve_page_folder(md_file, fm, entities)
    if target_dir is None:
        stats["review"] += 1
        _mark_needs_review(md_file)
        logger.info("[Connect] %s 无法自动分类，标记 needs_review 并留在 Inbox", md_file.name)
        return

    result = _move_page_to_category(md_file, target_dir, dry_run=dry_run)
    if result["status"] in ("moved", "already_there"):
        stats["moved"] += 1
        rel_target = target_dir.relative_to(Path(str(WIKI_DIR)))
        logger.info("[Connect] 已移动 %s -> %s/", md_file.name, rel_target.as_posix())
    elif result["status"] == "duplicate_basename":
        stats["review"] += 1
        _mark_needs_review(md_file)
        logger.warning(
            "[Connect] %s 与正式知识区已有同名页面，留在 Inbox 待审: %s",
            md_file.name,
            result.get("existing", []),
        )
    elif result["status"] == "collision":
        stats["review"] += 1
        logger.warning("[Connect] %s 目标文件冲突，留在 Inbox 待审", md_file.name)
    elif result["status"] == "error":
        stats["review"] += 1
        logger.warning(
            "[Connect] %s 移动失败: %s，留在 Inbox", md_file.name, result.get("error")
        )
    stats["classified"] += 1


def run_connect_cycle(
    dry_run: bool = False, db_path: str | Path | None = None, *, write_relations: bool = True
) -> Dict:
    """执行一轮关联构建 — 生成 Obsidian 知识图谱"""
    _ensure_dirs()

    if not INBOX_DIR.exists():
        logger.info("[Connect] 无 Inbox 目录，跳过")
        return {"people": 0, "projects": 0, "tech": 0, "concepts": 0, "mocs": 0}

    log_message = "构建 Obsidian 知识图谱..." if write_relations else "路由 Inbox 页面，跳过 KG 关系写入"
    logger.info("[Connect] %s", log_message)

    extractor = EntityExtractor(wiki_base=Path(str(WIKI_DIR)))
    _ = RelationEngine(db_path=db_path or (Path(str(WIKI_DIR)) / _KG_SUBDIR / "knowledge_graph.db"))

    all_entities, file_entities, project_tech = _init_connect_state()

    for md_file in INBOX_DIR.glob("*.md"):
        try:
            _process_inbox_file(
                md_file,
                extractor,
                all_entities,
                file_entities,
                project_tech,
                dry_run=dry_run,
                write_relations=write_relations,
            )
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at charon.py", exc_info=True
            )
            continue

    stats = {
        "people": 0,
        "projects": 0,
        "tech": 0,
        "concepts": 0,
        "mocs": 0,
        "retrospective": 0,
        "reports": 0,
        "classified": 0,
        "moved": 0,
        "review": 0,
    }

    if dry_run:
        _preview_dry_run(file_entities, all_entities)
        return stats

    # 实体页面生成已移除（原 generate_*_page / enrich_source_pages 为僵尸代码）
    # 关系分析仍在后台数据库维护，供搜索和画像使用
    logger.info("[Connect] 实体页面生成已禁用，仅更新关系数据库")

    # ========== 自动分类并移动文件 ==========
    for md_file, entities in file_entities.items():
        _handle_classified_file(md_file, entities, dry_run, stats)

    print(
        f"[Connect] 完成: {stats['people']} 人, {stats['projects']} 项目, "
        f"{stats['tech']} 技术, {stats['concepts']} 概念, {stats['mocs']} MOC, "
        f"{stats['retrospective']} 复盘, {stats['reports']} 报告, "
        f"分类 {stats['classified']} 篇, 移动 {stats['moved']} 篇, 待审 {stats['review']} 篇"
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Connect Worker - L2 to L3 Knowledge Graph")
    parser.add_argument("--watch", action="store_true", help="守护模式，每10分钟执行")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    add_periodic_loop_args(parser, default_interval=MAIN_SLEEP)
    args = parser.parse_args()

    if args.watch:
        logger.info("[Connect] 守护模式启动")

        def _cycle() -> None:
            logger.info("\n=== %s ===", datetime.now().isoformat())
            run_connect_cycle(dry_run=args.dry_run)

        run_periodic_loop(
            _cycle,
            interval=args.interval,
            max_cycles=resolve_max_cycles(once=args.once, max_cycles=args.max_cycles),
            run_seconds=args.run_seconds,
        )
    else:
        stats = run_connect_cycle(dry_run=args.dry_run)
        logger.info("\n=== 知识图谱构建完成 ===")
        for k, v in stats.items():
            logger.info("  %s: %s", k, v)


if __name__ == "__main__":
    main()
