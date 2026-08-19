"""Cognitive decision asset primitives for the Ixion flywheel."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

COGNITIVE_DECISION_ASSET_SCHEMA_VERSION = "cognitive_decision_asset.v1"
COGNITIVE_DECISION_ASSET_TYPES = {
    "methodology",
    "pitfall_pattern",
    "decision_heuristic",
    "verification_recipe",
    "automation_skill_candidate",
}

BEHAVIOR_DRIVEN_TIME_WINDOW_DAYS = 30
SKILL_WIKI_FLYWHEEL_DURATION_BUCKET_MONTH_DAYS = 30


@dataclass
class FlywheelInsight:
    """Flywheel signal emitted by Wiki, behavior, or skill usage analysis."""

    direction: str
    source: str
    target: str
    confidence: float
    reason: str
    suggested_action: str = ""
    auto_applicable: bool = False
    schema_version: str = COGNITIVE_DECISION_ASSET_SCHEMA_VERSION
    asset_type: str = "methodology"
    evidence_refs: List[str] = field(default_factory=list)
    applicability: List[str] = field(default_factory=list)
    failure_modes: List[str] = field(default_factory=list)
    verification_recipe: List[str] = field(default_factory=list)
    automation_derivative_allowed: bool = False


@dataclass
class CognitiveDecisionAsset:
    """Auditable decision asset produced before any automation skill is derived."""

    asset_id: str
    title: str
    asset_type: str = "methodology"
    decision_context: str = ""
    source_refs: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    applicability: List[str] = field(default_factory=list)
    failure_modes: List[str] = field(default_factory=list)
    verification_recipe: List[str] = field(default_factory=list)
    automation_derivative_allowed: bool = False
    status: str = "produced"
    confidence: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    schema_version: str = COGNITIVE_DECISION_ASSET_SCHEMA_VERSION

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "title": self.title,
            "asset_type": self.asset_type,
            "decision_context": self.decision_context,
            "source_refs": self.source_refs,
            "evidence_refs": self.evidence_refs,
            "applicability": self.applicability,
            "failure_modes": self.failure_modes,
            "verification_recipe": self.verification_recipe,
            "automation_derivative_allowed": self.automation_derivative_allowed,
            "status": self.status,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class BehaviorDrivenSkillGenerator:
    """Behavior-driven cognitive decision asset generator."""

    BEHAVIOR_TRIGGERS = {
        "min_occurrences": 3,
        "time_window_days": BEHAVIOR_DRIVEN_TIME_WINDOW_DAYS,
        "wiki_jaccard_threshold": 0.7,
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.behavior_triggers = dict(self.BEHAVIOR_TRIGGERS)

    def configure(self, cfg: Dict[str, Any]) -> None:
        for key in ("min_occurrences", "time_window_days", "wiki_jaccard_threshold"):
            if key in cfg:
                self.behavior_triggers[key] = cfg[key]

    def analyze(self) -> List[FlywheelInsight]:
        if not self.db_path.exists():
            return []
        since = (
            datetime.now() - timedelta(days=int(self.behavior_triggers["time_window_days"]))
        ).isoformat()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row  # noqa
            self._init_task_history(conn)
            rows = conn.execute(
                """SELECT task_type, subtype, COUNT(*) AS cnt
                   FROM task_history
                   WHERE completed_at >= ?
                   GROUP BY task_type, subtype
                   HAVING cnt >= ?""",
                (since, int(self.behavior_triggers["min_occurrences"])),
            ).fetchall()

        insights = []
        for row in rows:
            target = f"{row['task_type']}/{row['subtype']} 认知决策资产"
            insights.append(
                FlywheelInsight(
                    direction="behavior_to_cognitive_decision",
                    source=f"{row['task_type']}/{row['subtype']}",
                    target=target,
                    confidence=0.75,
                    reason=(
                        f"近{self.behavior_triggers['time_window_days']}天重复完成"
                        f" {row['cnt']} 次同类任务；优先提炼判断标准、失败边界和验证 recipe，"
                        "而不是直接脚本化"
                    ),
                    suggested_action=(
                        "生成 cognitive_decision_asset.v1，验证稳定后才允许派生 automation skill"
                    ),
                    auto_applicable=True,
                    asset_type="verification_recipe",
                    evidence_refs=[f"task_history:{row['task_type']}/{row['subtype']}"],
                    applicability=[f"{row['task_type']}/{row['subtype']}"],
                    failure_modes=["重复任务可能隐藏验证遗漏或返工模式"],
                    verification_recipe=[
                        "抽样复盘最近同类任务，确认共同失败点和成功路径",
                        "写明适用/不适用条件，再决定是否派生 automation skill",
                    ],
                    automation_derivative_allowed=False,
                )
            )
        return insights

    @staticmethod
    def _init_task_history(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                subtype TEXT NOT NULL,
                wiki_pages TEXT DEFAULT '[]',
                input_summary TEXT DEFAULT '',
                output_summary TEXT DEFAULT '',
                completed_at TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_history)")}
        for name, ddl in {
            "wiki_pages": "TEXT DEFAULT '[]'",
            "input_summary": "TEXT DEFAULT ''",
            "output_summary": "TEXT DEFAULT ''",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE task_history ADD COLUMN {name} {ddl}")
        conn.commit()


class CognitiveDecisionAssetMixin:
    """Wiki analysis and persistence methods mixed into the Ixion flywheel."""

    wiki_base: Path
    WIKI_TO_COGNITIVE_DECISION_SIGNALS: Dict[str, Any]

    def _conn(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _extract_frontmatter(self, content: str) -> Dict:
        raise NotImplementedError

    def _extract_title(self, content: str) -> str:
        raise NotImplementedError

    def _extract_body(self, content: str) -> str:
        raise NotImplementedError

    def _get_wiki_usage(
        self, page_path: str, days: int = SKILL_WIKI_FLYWHEEL_DURATION_BUCKET_MONTH_DAYS
    ) -> int:
        raise NotImplementedError

    def _scan_all_wiki_pages(self) -> List[Path]:
        raise NotImplementedError

    def analyze_wiki_for_cognitive_decision(
        self, page_path: Path
    ) -> Optional[FlywheelInsight]:
        """Analyze a Wiki page and return a cognitive decision asset suggestion."""
        if not page_path.exists():
            return None

        content = page_path.read_text(encoding="utf-8")
        frontmatter = self._extract_frontmatter(content)
        title = self._extract_title(content)
        body = self._extract_body(content)
        usage_count = self._get_wiki_usage(str(page_path))

        signals = []
        confidence = 0.0

        if usage_count >= self.WIKI_TO_COGNITIVE_DECISION_SIGNALS["min_usage_count"]:
            signals.append(f"使用次数 {usage_count} 次")
            confidence += 0.3
        elif usage_count >= 2:
            signals.append(f"使用次数 {usage_count} 次，有使用迹象")
            confidence += 0.05

        created_at = frontmatter.get("创建日期") or frontmatter.get("created_at")
        if created_at:
            try:
                age_days = (datetime.now() - datetime.fromisoformat(str(created_at))).days
                if age_days >= self.WIKI_TO_COGNITIVE_DECISION_SIGNALS["min_age_days"]:
                    signals.append(f"已沉淀 {age_days} 天")
                    confidence += 0.2
            except ValueError:
                pass

        page_confidence = float(frontmatter.get("置信度", frontmatter.get("confidence", 0)))
        if page_confidence >= self.WIKI_TO_COGNITIVE_DECISION_SIGNALS["min_confidence"]:
            signals.append(f"页面置信度 {page_confidence}")
            confidence += 0.2

        form = str(frontmatter.get("类型", frontmatter.get("type", "")))
        reusable_forms = ["方法论", "经验法则", "流程", "模板", "检查清单", "反模式", "决策"]
        if any(t in form for t in reusable_forms):
            signals.append(f"可复用类型: {form}")
            confidence += 0.2

        if re.search(r"(步骤|流程|检查|清单|判断|边界|验证|反模式|取舍|第[一二三四五]步|\d+\.)", body):
            signals.append("包含可复用判断/步骤/边界/验证结构")
            confidence += 0.1

        if confidence < 0.5:
            return None

        asset_type = self._asset_type_from_form(form, body)
        return FlywheelInsight(
            direction="wiki_to_cognitive_decision",
            source=str(page_path),
            target=self._suggest_cognitive_asset_title(title),
            confidence=min(confidence, 1.0),
            reason="; ".join(signals),
            suggested_action=self._generate_cognitive_asset_proposal(
                page_path, frontmatter, title, body
            ),
            auto_applicable=True,
            schema_version=COGNITIVE_DECISION_ASSET_SCHEMA_VERSION,
            asset_type=asset_type,
            evidence_refs=[str(page_path), *[f"wiki_usage:{usage_count}"]],
            applicability=list(frontmatter.get("触发场景", []))[:5],
            failure_modes=self._infer_failure_modes(body),
            verification_recipe=self._infer_verification_recipe(body),
            automation_derivative_allowed=False,
        )

    def scan_wiki_for_cognitive_decision_assets(self) -> List[FlywheelInsight]:
        """Scan the full Wiki vault for cognitive decision asset candidates."""
        insights = []
        for page in self._scan_all_wiki_pages():
            insight = self.analyze_wiki_for_cognitive_decision(page)
            if insight:
                insights.append(insight)
        insights.sort(key=lambda x: x.confidence, reverse=True)
        return insights

    def _generate_cognitive_asset_proposal(
        self, page_path: Path, frontmatter: Dict, title: str, body: str
    ) -> str:
        """Generate a cognitive decision asset proposal."""
        scenes = frontmatter.get("触发场景", ["未指定场景"])
        tools = frontmatter.get("关键词", {}).get("工具实体", [])
        return f"""建议将知识沉淀为 cognitive_decision_asset.v1：

**资产标题**: {self._suggest_cognitive_asset_title(title)}
**来源**: {page_path.name}
**资产类型**: {self._asset_type_from_form(str(frontmatter.get("类型", "")), body)}

**适用条件**:
- {'; '.join(scenes[:3])}

**判断标准**:
- 从原文提取判断条件、取舍依据和不适用边界

**验证 recipe**:
- 用一个新案例验证判断标准是否仍成立
- 记录失败样本、用户纠正和后续回流指标

**automation skill 派生条件**:
- 只有当资产已验证、边界清楚，并且{', '.join(tools[:2]) if tools else '相关操作'}足够稳定时，才允许派生 automation skill

**注意事项**:
- 原文中的适用边界必须写入资产
- 反模式、失败样本和验证遗漏必须作为资产字段保留
"""

    @staticmethod
    def _asset_type_from_form(form: str, body: str) -> str:
        if "反模式" in form or "反模式" in body or "坑" in body:
            return "pitfall_pattern"
        if "决策" in form or "取舍" in body or "判断" in body:
            return "decision_heuristic"
        if "验证" in body or "检查" in body or "验收" in body:
            return "verification_recipe"
        return "methodology"

    @staticmethod
    def _infer_failure_modes(body: str) -> List[str]:
        modes: List[str] = []
        if "反模式" in body:
            modes.append("原文包含反模式，执行前必须检查是否命中")
        if "边界" in body or "不适用" in body:
            modes.append("适用边界不清会导致错误迁移")
        if not modes:
            modes.append("缺少失败样本时不得直接派生 automation skill")
        return modes

    @staticmethod
    def _infer_verification_recipe(body: str) -> List[str]:
        recipe = ["用一个新案例复用该判断标准并记录结果"]
        if "测试" in body or "验证" in body or "检查" in body:
            recipe.append("沿用原文验证/检查步骤并记录通过率")
        recipe.append("若出现失败样本，先补充失败边界，再考虑自动化")
        return recipe

    @staticmethod
    def _suggest_cognitive_asset_title(title: str) -> str:
        clean = re.sub(r"^(如何|怎样|怎么|为什么)", "", title).strip()
        if not clean:
            clean = "未命名认知决策资产"
        if "认知决策资产" not in clean:
            clean += "认知决策资产"
        return clean

    def _suggest_skill_name(self, title: str) -> str:
        """Compatibility name suggestion for callers that still ask for a skill."""
        name = re.sub(r"^(为什么|怎么|如何|什么是)", "", title).strip()
        return name if name.endswith("助手") else name + "助手"

    @staticmethod
    def _asset_id_from(title: str, source: str = "") -> str:
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", title).strip("-")
        if not slug:
            slug = "cognitive-decision"
        digest = hashlib.sha1(
            (source or title).encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        return f"cda-{slug[:48]}-{digest[:8]}"

    def create_cognitive_decision_asset(self, asset: CognitiveDecisionAsset) -> bool:
        """Create or update a cognitive_decision_asset.v1 record."""
        if asset.asset_type not in COGNITIVE_DECISION_ASSET_TYPES:
            asset.asset_type = "methodology"
        now = datetime.now().isoformat()[:19]
        created_at = asset.created_at or now
        updated_at = asset.updated_at or now
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO cognitive_decision_assets
                   (asset_id, schema_version, title, asset_type, decision_context,
                    source_refs, evidence_refs, applicability, failure_modes,
                    verification_recipe, automation_derivative_allowed, status,
                    confidence, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    asset.asset_id,
                    asset.schema_version,
                    asset.title,
                    asset.asset_type,
                    asset.decision_context,
                    json.dumps(asset.source_refs, ensure_ascii=False),
                    json.dumps(asset.evidence_refs, ensure_ascii=False),
                    json.dumps(asset.applicability, ensure_ascii=False),
                    json.dumps(asset.failure_modes, ensure_ascii=False),
                    json.dumps(asset.verification_recipe, ensure_ascii=False),
                    1 if asset.automation_derivative_allowed else 0,
                    asset.status,
                    asset.confidence,
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()
        return True

    def get_cognitive_decision_asset(
        self, asset_id: str
    ) -> Optional[CognitiveDecisionAsset]:
        """Return a cognitive decision asset by id."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cognitive_decision_assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
        if not row:
            return None
        return self._asset_from_row(row)

    def list_cognitive_decision_assets(
        self, status: str | None = None
    ) -> List[CognitiveDecisionAsset]:
        """List cognitive decision assets, optionally filtered by lifecycle status."""
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM cognitive_decision_assets WHERE status=?", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM cognitive_decision_assets").fetchall()
        return [self._asset_from_row(row) for row in rows]

    @staticmethod
    def _asset_from_row(row: sqlite3.Row) -> CognitiveDecisionAsset:
        def load_list(name: str) -> List[str]:
            try:
                value = json.loads(row[name] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
            return value if isinstance(value, list) else []

        return CognitiveDecisionAsset(
            asset_id=row["asset_id"],
            schema_version=row["schema_version"],
            title=row["title"],
            asset_type=row["asset_type"],
            decision_context=row["decision_context"],
            source_refs=load_list("source_refs"),
            evidence_refs=load_list("evidence_refs"),
            applicability=load_list("applicability"),
            failure_modes=load_list("failure_modes"),
            verification_recipe=load_list("verification_recipe"),
            automation_derivative_allowed=bool(row["automation_derivative_allowed"]),
            status=row["status"],
            confidence=float(row["confidence"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
