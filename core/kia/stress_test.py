"""
Knowledge Stress Test - 知识压力测试

主动挑战知识的边界条件，发现盲区：
1. 边界挑战 — 适用边界是否足够清晰？
2. 反例挑战 — 是否存在知识不适用的情况？
3. 极端场景 — 极端条件下知识是否仍然成立？
4. 组合挑战 — 多个知识同时作用时的冲突？
5. 时效挑战 — 知识是否可能已过时？

挑战来源：
- 基于 frontmatter 的适用边界和反模式生成
- 基于知识类型使用预设挑战模板
- 可选接入 LLM 生成深度挑战（接口预留）

输出：挑战清单 + 知识韧性评分
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
from core.cognitive.state_contract import sha256_json
from core.pluggable import PluggableModule

import logging

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency fallback
    yaml = None

from core.utils import LazyPath  # noqa: E402
from core.trust.formal_markdown import (  # noqa: E402
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text  # noqa: E402

STRESS_TEST_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:knowledge-stress-frontmatter",
    contract_revision_id="mnemos.knowledge_stress_frontmatter.v1",
    contract_text=(
        "StressTestEngine may project only the exact persisted stress-test result "
        "onto the exact tested page preimage."
    ),
    source_namespace="knowledge-stress-frontmatter",
    producer="knowledge-stress-test",
    producer_code_hash=sha256_json(
        {
            "module": "core.kia.stress_test",
            "producer": "_update_page_frontmatter",
            "version": "mnemos.knowledge_stress_frontmatter.v1",
        }
    ),
    evaluator_id="knowledge-stress-frontmatter-evaluator",
    constraints=(
        "Target, page preimage, score, challenge counts, blind spots, and output remain exact.",
        "The projection may not alter the underlying persisted stress-test result.",
    ),
    approved_candidate_key="project_exact_stress_result",
    approved_candidate_summary="Project the exact stress-test result to its tested page.",
    rejected_candidate_key="retain_page_without_stress_projection",
    rejected_candidate_summary="Retain the page when result or page binding drifts.",
    approved_reason_code="stress_result_binding_verified",
    rejected_reason_code="stress_result_binding_rejected",
    committed_metric="stress_result_frontmatter_committed",
    rejected_metric="unbound_stress_result_frontmatter_count",
)

# Constants extracted from magic numbers
STRESS_TEST_ENGINE__CALCULATE_RESILIENCE_TZINFO_DAYS = 90
STRESS_TEST_ENGINE__DAYS_SINCE_LAST_TEST = 9999


DB_PATH = LazyPath("database_dir", "stress_test.db")
WIKI_DIR = LazyPath("wiki_dir")
EXCLUDED_DIRS = {
    ".git",
    ".obsidian",
    "__pycache__",
    "99-Reports",
    "reports",
    "shadow_pages",
    ".shadow_pages",
}


# ==================== Data Classes ====================


@dataclass
class Challenge:
    """挑战项"""

    challenge_type: str  # boundary / counter_example / extreme / combination / temporal
    question: str  # 挑战问题
    expected_behavior: str = ""  # 知识在此场景下应如何表现
    risk_level: str = "medium"  # low / medium / high
    triggered_by: str = ""  # 触发这个挑战的知识特征


@dataclass
class StressTestResult:
    """压力测试结果"""

    page_path: str
    page_title: str = ""
    resilience_score: float = 0.0  # 0-10，知识韧性评分
    challenges: List[Challenge] = field(default_factory=list)
    passed_challenges: int = 0
    failed_challenges: int = 0
    blind_spots: List[str] = field(default_factory=list)


# ==================== StressTestEngine ====================


class StressTestEngine(PluggableModule):
    """知识压力测试引擎 — 实现 PluggableModule 热插拔接口"""

    # 挑战模板（按知识类型）
    CHALLENGE_TEMPLATES = {
        "问题-解决": [
            {
                "type": "boundary",
                "template": "如果问题发生在 {scenario} 之外的环境中，这个解决方案是否仍然有效？",
                "risk": "medium",
            },
            {
                "type": "counter_example",
                "template": "是否存在 {tool} 的版本/配置使得这个解决方案失效？",
                "risk": "high",
            },
            {
                "type": "extreme",
                "template": "如果问题规模扩大 100 倍（数据量/并发量/用户数），这个方案是否还能工作？",
                "risk": "high",
            },
        ],
        "经验法则": [
            {
                "type": "boundary",
                "template": "这条经验在什么规模/团队/技术栈下不再适用？",
                "risk": "medium",
            },
            {
                "type": "counter_example",
                "template": "是否有知名项目/团队违反这条经验但仍取得成功？",
                "risk": "medium",
            },
            {
                "type": "extreme",
                "template": "如果严格遵守这条经验，是否会在某些场景下造成过度设计？",
                "risk": "low",
            },
        ],
        "决策记录": [
            {
                "type": "boundary",
                "template": "当初决策的假设条件（如团队规模、预算、时间）如果发生变化，决策结果是否会反转？",
                "risk": "high",
            },
            {
                "type": "temporal",
                "template": "距离决策已过去一段时间，是否有新的信息/技术出现使得原决策需要重新评估？",
                "risk": "medium",
            },
            {
                "type": "combination",
                "template": "这个决策是否与其他已做过的决策存在隐性冲突？",
                "risk": "medium",
            },
        ],
        "反模式": [
            {
                "type": "boundary",
                "template": "是否存在任何场景下这个'反模式'实际上是正确做法？",
                "risk": "medium",
            },
            {
                "type": "extreme",
                "template": "在资源极度受限（时间/人力/预算）时，是否不得不采用这个反模式？",
                "risk": "low",
            },
        ],
        "方法论": [
            {
                "type": "boundary",
                "template": "这个方法论的步骤是否可以跳过/简化？什么情况下可以？",
                "risk": "low",
            },
            {
                "type": "extreme",
                "template": "如果只有原定时限的 1/3，这个方法论如何调整？",
                "risk": "high",
            },
            {
                "type": "combination",
                "template": "同时运行多个方法论时，步骤之间是否存在冲突或重复？",
                "risk": "medium",
            },
        ],
        "洞察关联": [
            {
                "type": "counter_example",
                "template": "是否有反例证明这两个事物之间不存在关联？",
                "risk": "high",
            },
            {
                "type": "boundary",
                "template": "这种关联在什么条件下会断裂/失效？",
                "risk": "medium",
            },
        ],
    }

    DEFAULT_CHALLENGE_TEMPLATES = [
        {
            "type": "boundary",
            "template": "这条知识的适用边界是什么？在什么条件下它可能不适用或产生反效果？",
            "risk": "medium",
        },
        {
            "type": "temporal",
            "template": "这条知识的时效性如何？随着时间推移，有哪些因素可能导致它失效？",
            "risk": "medium",
        },
    ]

    def __init__(
        self, wiki_base: str | None = None, db_path: str | Path | None = None, dry_run: bool = False
    ):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            self.wiki_base = WIKI_DIR  # type: ignore[assignment]
        self.inbox = self.wiki_base / "00-Inbox"
        self._db_path = Path(db_path).expanduser() if db_path else DB_PATH
        self.dry_run = dry_run
        self._init_db()
        self._enabled = True
        # 防止 knowledge_needs_reinforcement 事件造成自环重入
        self._reinforcement_lock = threading.Lock()
        self._reinforcement_in_flight: set[str] = set()

    # ---- PluggableModule 接口 ----

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def configure(self, cfg: Dict[str, Any]) -> None:
        if "thresholds" in cfg:
            self.THRESHOLDS.update(cfg["thresholds"])  # type: ignore[attr-defined]

    def handle_event(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        if event_type == "periodic_stress_test":
            limit = data.get("limit", 10)
            self.batch_test(limit=limit)
        elif event_type == "page_created":
            page_path = data.get("page_path")
            if page_path:
                self.test_page(Path(page_path))
        elif event_type == "knowledge_needs_reinforcement":
            page_path = data.get("page_path")
            if not page_path:
                return
            path_key = str(page_path)
            with self._reinforcement_lock:
                if path_key in self._reinforcement_in_flight:
                    logger.debug("[StressTest] 跳过已在处理中的加固事件: %s", path_key)
                    return
                self._reinforcement_in_flight.add(path_key)
            try:
                self.test_page(Path(page_path))
            finally:
                with self._reinforcement_lock:
                    self._reinforcement_in_flight.discard(path_key)

    # ---- DB helpers ----

    def _get_conn(self) -> sqlite3.Connection:
        db = Path(str(self._db_path))
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), timeout=10)
        conn.row_factory = sqlite3.Row  # noqa
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stress_test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_path TEXT NOT NULL,
                page_title TEXT,
                resilience_score REAL,
                challenges_count INTEGER,
                passed_challenges INTEGER DEFAULT 0,
                failed_challenges INTEGER DEFAULT 0,
                blind_spots_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_str_page ON stress_test_results(page_path);
            CREATE TABLE IF NOT EXISTS stress_test_challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                challenge_type TEXT,
                question TEXT,
                expected_behavior TEXT,
                risk_level TEXT,
                triggered_by TEXT,
                FOREIGN KEY (result_id) REFERENCES stress_test_results(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_stc_result ON stress_test_challenges(result_id);
        """)
        self._ensure_result_outcome_columns(conn)
        conn.commit()
        conn.close()

    @staticmethod
    def _ensure_result_outcome_columns(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(stress_test_results)").fetchall()
        }
        if "passed_challenges" not in columns:
            conn.execute(
                "ALTER TABLE stress_test_results "
                "ADD COLUMN passed_challenges INTEGER DEFAULT 0"
            )
        if "failed_challenges" not in columns:
            conn.execute(
                "ALTER TABLE stress_test_results "
                "ADD COLUMN failed_challenges INTEGER DEFAULT 0"
            )

    def save_result(self, result: StressTestResult):
        """保存测试结果与挑战详情到 SQLite"""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO stress_test_results
                   (page_path, page_title, resilience_score,
                    challenges_count, passed_challenges, failed_challenges,
                    blind_spots_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.page_path,
                    result.page_title,
                    result.resilience_score,
                    len(result.challenges),
                    result.passed_challenges,
                    result.failed_challenges,
                    len(result.blind_spots),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            result_id = cur.lastrowid
            for c in result.challenges:
                conn.execute(
                    """INSERT INTO stress_test_challenges
                       (result_id, challenge_type, question, expected_behavior,
                        risk_level, triggered_by)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        result_id,
                        c.challenge_type,
                        c.question,
                        c.expected_behavior,
                        c.risk_level,
                        c.triggered_by,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    # ---- Core Logic ----

    def test_page(
        self, page_path: Path, use_llm: bool = False, dry_run: bool | None = None
    ) -> StressTestResult:
        """
        对单个页面进行压力测试

        Args:
            page_path: 知识页面路径
            use_llm: 是否使用 LLM 生成深度挑战（预留接口）
            dry_run: 为 True 时不写入 DB 和修改 frontmatter；
                     默认 None 表示使用实例级 self.dry_run

        Returns:
            StressTestResult
        """
        if dry_run is None:
            dry_run = self.dry_run
        result = StressTestResult(page_path=str(page_path))

        if not page_path.exists():
            return result

        loaded = self._load_page(page_path)
        if loaded is None:
            return result
        content, fm, body = loaded

        result.page_title = self._extract_title(content) or page_path.stem

        result.challenges.extend(self._build_template_challenges(fm))

        boundaries = self._extract_boundaries(body, fm)
        result.challenges.extend(self._build_boundary_challenges(boundaries))

        anti_patterns = self._extract_anti_patterns(body, fm)
        boundaries["anti_patterns"] = anti_patterns
        result.challenges.extend(self._build_anti_pattern_challenges(anti_patterns))

        result.challenges.extend(self._build_temporal_challenges(fm))
        result.challenges.extend(self._build_confidence_challenge(fm))

        result.resilience_score = self._calculate_resilience(result, fm, boundaries)
        result.blind_spots = self._identify_blind_spots(result, fm, boundaries)
        self._finalize_challenge_outcomes(result)

        if not dry_run:
            self.save_result(result)
            self._update_page_frontmatter(page_path, result)

        self._emit_stress_events(page_path, result)

        return result

    def _load_page(self, page_path: Path) -> Optional[tuple]:
        """读取并解析单个页面内容。"""
        try:
            content = page_path.read_text(encoding="utf-8")
            return content, self._extract_frontmatter(content), self._extract_body(content)
        except (OSError, IOError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at stress_test.py", exc_info=True
            )
            return None

    def _build_template_challenges(self, fm: Dict) -> List[Challenge]:
        """基于知识类型模板生成挑战。"""
        form = self._fm_get(fm, "类型", "")
        templates = self.CHALLENGE_TEMPLATES.get(form) or self.DEFAULT_CHALLENGE_TEMPLATES

        tools = self._get_keywords(fm, "工具实体")
        scenarios = self._get_keywords(fm, "场景标签")
        domains = self._get_keywords(fm, "领域")
        tool_str = tools[0] if tools else "相关工具"
        scenario_str = scenarios[0] if scenarios else "当前场景"
        domain_str = self._fm_get(fm, "领域", domains[0] if domains else "通用领域")
        confidence_str = str(self._fm_get(fm, "置信度", 0.5))
        version_str = self._fm_get(fm, "版本标记", "未标注版本")

        challenges = []
        for template in templates:
            question = template["template"].format(
                tool=tool_str,
                scenario=scenario_str,
                domain=domain_str,
                confidence=confidence_str,
                version=version_str,
            )
            challenges.append(
                Challenge(
                    challenge_type=template["type"],
                    question=question,
                    risk_level=template.get("risk", "medium"),
                    triggered_by=f"知识类型: {form}",
                )
            )
        return challenges

    @staticmethod
    def _build_boundary_challenges(boundaries: Dict) -> List[Challenge]:
        """基于适用边界生成挑战。"""
        challenges = []
        if boundaries.get("applies"):
            challenges.append(
                Challenge(
                    challenge_type="boundary",
                    question=f"适用条件声明为「{boundaries['applies']}」，但这个声明本身是否完整？是否有遗漏的隐含条件？",
                    risk_level="high",
                    triggered_by="适用边界",
                )
            )
        if boundaries.get("not_applies"):
            challenges.append(
                Challenge(
                    challenge_type="boundary",
                    question=f"不适用场景声明为「{boundaries['not_applies']}」，是否还有其他未声明的不适用场景？",
                    risk_level="medium",
                    triggered_by="不适用边界",
                )
            )
        return challenges

    @staticmethod
    def _build_anti_pattern_challenges(anti_patterns: List[str]) -> List[Challenge]:
        """基于反模式生成挑战。"""
        challenges = []
        for anti in anti_patterns[:2]:
            challenges.append(
                Challenge(
                    challenge_type="counter_example",
                    question=f"反模式「{anti[:50]}...」是否有任何例外情况？",
                    risk_level="medium",
                    triggered_by="反模式",
                )
            )
        return challenges

    def _build_temporal_challenges(self, fm: Dict) -> List[Challenge]:
        """基于时效性生成挑战。"""
        temporal = self._fm_get(fm, "时效性", "")
        version_tag = self._fm_get(fm, "版本标记", "")
        if temporal == "版本绑定" and version_tag:
            return [
                Challenge(
                    challenge_type="temporal",
                    question=f"版本标记为「{version_tag}」，该版本是否有已知的弃用计划或重大变更？",
                    risk_level="high",
                    triggered_by="版本绑定",
                )
            ]
        if temporal == "上下文相关":
            return [
                Challenge(
                    challenge_type="temporal",
                    question="上下文相关知识容易随环境变化而失效，当前上下文是否已发生变化？",
                    risk_level="medium",
                    triggered_by="上下文相关",
                )
            ]
        return []

    def _build_confidence_challenge(self, fm: Dict) -> List[Challenge]:
        """基于置信度生成挑战。"""
        confidence = float(self._fm_get(fm, "置信度", 0.5))
        if confidence < 0.6:
            return [
                Challenge(
                    challenge_type="boundary",
                    question=f"置信度仅 {confidence}，这意味着知识可靠性不足。什么条件下可以提升到 0.8 以上？",
                    risk_level="high",
                    triggered_by="低置信度",
                )
            ]
        return []

    @staticmethod
    def _finalize_challenge_outcomes(result: StressTestResult) -> None:
        failed = len(result.blind_spots)
        result.failed_challenges = failed
        result.passed_challenges = max(len(result.challenges) - failed, 0)

    def _emit_stress_events(self, page_path: Path, result: StressTestResult):
        """发布压力测试相关事件。"""
        path_key = str(page_path)
        if (
            result.resilience_score is not None
            and result.resilience_score < 5.0
            and path_key not in self._reinforcement_in_flight
        ):
            self._emit_event(
                "knowledge_needs_reinforcement",
                {
                    "page_path": path_key,
                    "score": result.resilience_score,
                    "blind_spots": result.blind_spots,
                },
            )
        if result.blind_spots:
            for bs in result.blind_spots:
                self._emit_event(
                    "profile_blindspot_detected",
                    {
                        "page_path": str(page_path),
                        "blindspot": bs,
                    },
                )

    def batch_test(
        self,
        limit: int | None = None,
        filter_fn: Optional[Callable[[Path], bool]] = None,
        dry_run: bool = False,
    ) -> List[StressTestResult]:
        """批量测试所有知识页面"""
        results = []

        pages = self._list_pages()

        for page in pages:
            if filter_fn and not filter_fn(page):
                continue
            result = self.test_page(page, dry_run=dry_run)
            if result.challenges:
                results.append(result)
            if limit and len(results) >= limit:
                break

        return results

    def _calculate_resilience(
        self, result: StressTestResult, frontmatter: Dict, boundaries: Dict
    ) -> float:
        """计算知识韧性评分 0-10"""
        score = 5.0  # 基础分

        # 有明确适用边界 +2
        if boundaries.get("applies") and boundaries.get("not_applies"):
            score += 2
        elif boundaries.get("applies") or boundaries.get("not_applies"):
            score += 1

        # 有反模式 +1
        if self._fm_get(frontmatter, "类型") == "反模式" or boundaries.get("anti_patterns"):
            score += 1

        # 置信度高 +1
        confidence = float(self._fm_get(frontmatter, "置信度", 0))
        if confidence >= 0.8:
            score += 1
        elif confidence < 0.5:
            score -= 1

        # 高风险的挑战多 → 扣分
        high_risk = sum(1 for c in result.challenges if c.risk_level == "high")
        score -= high_risk * 0.3

        # 挑战数量适中最好（太少说明知识太简单，太多说明知识太脆弱）
        if len(result.challenges) < 2:
            score -= 0.5
        elif len(result.challenges) > 8:
            score -= 0.5

        last_test = self._fm_get(frontmatter, "上次压力测试", "")
        if last_test:
            try:
                last_dt = datetime.fromisoformat(str(last_test))
                if (
                    datetime.now() - last_dt.replace(tzinfo=None)
                ).days > STRESS_TEST_ENGINE__CALCULATE_RESILIENCE_TZINFO_DAYS:
                    score -= 0.5
            except ValueError:
                logger.warning("[stress_test] ValueError suppressed", exc_info=True)

        return max(0, min(10, round(score, 1)))

    def _identify_blind_spots(
        self, result: StressTestResult, frontmatter: Dict, boundaries: Dict
    ) -> List[str]:
        """识别知识盲区"""
        blind_spots = []

        # 缺少适用边界
        if not boundaries.get("applies") and not boundaries.get("not_applies"):
            blind_spots.append("未声明适用边界，可能导致误用")

        # 缺少反模式
        if self._fm_get(frontmatter, "类型") in ["经验法则", "方法论"] and not boundaries.get(
            "anti_patterns"
        ):
            blind_spots.append("缺少反模式/注意事项，未考虑失败场景")

        # 低置信度
        confidence = float(self._fm_get(frontmatter, "置信度", 0.5))
        if confidence < 0.5:
            blind_spots.append("置信度过低，知识基础不牢")

        # 单源证据
        if self._fm_get(frontmatter, "证据级别") == "单源":
            blind_spots.append("单源证据，未经交叉验证")

        # 时效性风险
        if self._fm_get(frontmatter, "时效性") == "版本绑定" and not self._fm_get(
            frontmatter, "版本标记"
        ):
            blind_spots.append("声明为版本绑定但未标注版本号")

        return blind_spots

    def generate_report(self, result: StressTestResult) -> str:
        """生成压力测试报告 Markdown"""
        lines = [
            f"# 压力测试报告: {result.page_title}",
            f"韧性评分: **{result.resilience_score:.1f}** / 10",
            f"挑战数量: {len(result.challenges)}",
            f"挑战通过: {result.passed_challenges}",
            f"挑战失败: {result.failed_challenges}",
            "",
        ]

        if result.blind_spots:
            lines.extend(["## 盲区", ""])
            for spot in result.blind_spots:
                lines.append(f"- ⚠️ {spot}")
            lines.append("")

        # 按风险等级分组
        risk_order = {"high": "🔴", "medium": "🟠", "low": "🟢"}
        for risk in ["high", "medium", "low"]:
            challenges = [c for c in result.challenges if c.risk_level == risk]
            if not challenges:
                continue
            lines.extend([f"## {risk_order[risk]} {risk.upper()} 风险挑战", ""])
            for i, c in enumerate(challenges, 1):
                lines.append(f"{i}. **[{c.challenge_type}]** {c.question}")
                lines.append(f"   触发原因: {c.triggered_by}")
                lines.append("")

        return "\n".join(lines)

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        if yaml is None:
            return {}
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except (yaml.YAMLError, ValueError):
                    logging.getLogger(__name__).warning(
                        "Caught unexpected error at stress_test.py", exc_info=True
                    )
                    return {}
        return {}

    @staticmethod
    def _extract_body(content: str) -> str:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2]
        return content

    @staticmethod
    def _extract_title(content: str) -> str:
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _get_keywords(frontmatter: Dict, layer: str) -> List[str]:
        keywords = frontmatter.get("关键词", {})
        if isinstance(keywords, dict):
            return keywords.get(layer, []) or []
        return []

    def _extract_boundaries(self, body: str, frontmatter: Optional[Dict] = None) -> Dict:
        """提取适用边界，支持 frontmatter、列表、标题段落和内联标记。"""
        frontmatter = frontmatter or {}
        boundaries = {}

        for field_name in ["适用条件", "适用边界", "适用范围"]:
            value = frontmatter.get(field_name)
            if value:
                boundaries["applies"] = self._normalize_field_value(value)
                break

        for field_name in ["不适用场景", "不适用条件", "不适用边界"]:
            value = frontmatter.get(field_name)
            if value:
                boundaries["not_applies"] = self._normalize_field_value(value)
                break

        if not boundaries.get("applies"):
            boundaries["applies"] = self._first_pattern_match(
                body,
                [
                    r"(?m)^\s*[\-\*]\s+适用(?:于)?[：:]\s*(.+?)(?:\n\s*[\-\*]\s+|\n#{1,3}\s|\Z)",
                    r"#{1,3}\s*(?:适用边界|适用范围|适用条件)\s*\n(.+?)(?:\n#{1,3}\s|\Z)",
                    r"\*\*适用[：:]\*\*\s*(.+?)(?:\n|\Z)",
                ],
            )

        if not boundaries.get("not_applies"):
            boundaries["not_applies"] = self._first_pattern_match(
                body,
                [
                    r"(?m)^\s*[\-\*]\s+不适用(?:于)?[：:]\s*(.+?)(?:\n\s*[\-\*]\s+|\n#{1,3}\s|\Z)",
                    r"#{1,3}\s*(?:不适用边界|不适用范围|不适用条件|不适用场景)\s*\n(.+?)(?:\n#{1,3}\s|\Z)",
                    r"\*\*不适用[：:]\*\*\s*(.+?)(?:\n|\Z)",
                ],
            )

        return boundaries

    def _extract_anti_patterns(self, body: str, frontmatter: Optional[Dict] = None) -> List[str]:
        """提取反模式列表，支持 frontmatter 和正文 section。"""
        frontmatter = frontmatter or {}
        patterns = []  # type: ignore[var-annotated]

        for field_name in ["反模式", "注意事项", "常见错误", "避坑指南"]:
            value = frontmatter.get(field_name)
            if value:
                if isinstance(value, list):
                    patterns.extend(str(item).strip() for item in value if str(item).strip())
                else:
                    patterns.append(str(value).strip())

        if not patterns:
            section_match = re.search(
                r"#{1,3}\s*(?:反模式|注意事项|常见错误|避坑指南)\s*\n(.+?)(?:\n#{1,3}\s|\Z)",
                body,
                re.DOTALL | re.IGNORECASE,
            )
            if section_match:
                section = section_match.group(1)
                for line in section.split("\n"):
                    line = line.strip()
                    if line.startswith(("-", "*")) or re.match(r"^\d+\.", line):
                        text = re.sub(r"^(?:[-*]|\d+\.)\s*", "", line).strip()
                        if text and len(text) > 5:
                            patterns.append(text)

        return patterns[:5]

    def _list_pages(self) -> List[Path]:
        wiki_base = Path(str(self.wiki_base))
        if not wiki_base.exists():
            return []
        pages = []
        for page in wiki_base.rglob("*.md"):
            relative_parts = set(page.relative_to(wiki_base).parts[:-1])
            if relative_parts & EXCLUDED_DIRS:
                continue
            pages.append(page)
        return sorted(pages, key=self._page_priority)

    def _page_priority(self, page: Path) -> tuple:
        last_age = self._days_since_last_test(page)
        type_rank = 1
        try:
            fm = self._extract_frontmatter(page.read_text(encoding="utf-8"))
            if self._fm_get(fm, "类型") in {"决策记录", "方法论"}:
                type_rank = 0
        except (OSError, IOError):
            logging.getLogger(__name__).warning("Caught unexpected error", exc_info=True)
        return (-last_age, type_rank, str(page))

    def _days_since_last_test(self, page_path: Path) -> int:
        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT created_at FROM stress_test_results
                   WHERE page_path=? ORDER BY created_at DESC LIMIT 1""",
                (str(page_path),),
            ).fetchone()
        if not row:
            return STRESS_TEST_ENGINE__DAYS_SINCE_LAST_TEST
        try:
            last = datetime.fromisoformat(str(row["created_at"]))
            return (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).days
        except ValueError:
            return STRESS_TEST_ENGINE__DAYS_SINCE_LAST_TEST

    @staticmethod
    def _fm_get(frontmatter: Dict, key: str, default: Any | None = None) -> Any:
        value = frontmatter.get(key, default)
        return default if value is None else value

    @staticmethod
    def _normalize_field_value(value: Any) -> str:
        if isinstance(value, list):
            return "；".join(str(item).strip() for item in value if str(item).strip())
        return str(value).strip()

    @staticmethod
    def _first_pattern_match(body: str, patterns: List[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, body, re.DOTALL | re.IGNORECASE)
            if match:
                text = match.group(1).strip()
                text = re.sub(r"\n+", " ", text)
                return text
        return ""

    def _update_page_frontmatter(self, page_path: Path, result: StressTestResult):
        if yaml is None:
            return

        content = page_path.read_text(encoding="utf-8")
        fm, body = self._split_frontmatter(content)
        fm["韧性评分"] = result.resilience_score
        fm["上次压力测试"] = datetime.now().date().isoformat()
        fm["盲区清单"] = result.blind_spots
        fm["通过挑战数"] = result.passed_challenges
        fm["失败挑战数"] = result.failed_challenges

        history = self._get_test_history(str(page_path), limit=2)
        if len(history) >= 2 and all(row["resilience_score"] < 4.0 for row in history):
            fm["需加固"] = True
            self._emit_event(
                "knowledge_needs_reinforcement",
                {
                    "page_path": str(page_path),
                    "score": result.resilience_score,
                    "blind_spots": result.blind_spots,
                },
            )

        evidence_refs = [f"stress_test:{page_path}"]
        submit_or_write_markdown_with_decision(
            decision_policy=STRESS_TEST_MARKDOWN_POLICY,
            decision_facts={
                "schema_version": "mnemos.knowledge_stress_result_facts.v1",
                "page_path": result.page_path,
                "page_title": result.page_title,
                "resilience_score": result.resilience_score,
                "passed_challenges": result.passed_challenges,
                "failed_challenges": result.failed_challenges,
                "blind_spots": list(result.blind_spots),
                "challenge_count": len(result.challenges),
                "requires_reinforcement": bool(fm.get("需加固", False)),
            },
            decision_task=f"Project stress-test result for {page_path.name}",
            decision_goal="Keep Wiki resilience metadata aligned with the exact test result.",
            decision_created_at=datetime.now(timezone.utc).isoformat(),
            wiki_base=self.wiki_base,
            target_path=page_path,
            content=self._join_frontmatter(fm, body),
            source="stress_test",
            actor="system",
            evidence_refs=evidence_refs,
            proposed_action="update_resilience_frontmatter",
            expected_existing_hash=sha256_text(content),
            metadata={"resilience_score": result.resilience_score},
        )

    def _get_test_history(self, page_path: str, limit: int = 2) -> List[sqlite3.Row]:
        with self._get_conn() as conn:
            return conn.execute(
                """SELECT resilience_score, created_at FROM stress_test_results
                   WHERE page_path=? ORDER BY created_at DESC LIMIT ?""",
                (page_path, limit),
            ).fetchall()

    @classmethod
    def _split_frontmatter(cls, content: str) -> tuple[Dict, str]:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return cls._extract_frontmatter(content), parts[2].lstrip("\n")
        return {}, content

    @staticmethod
    def _join_frontmatter(frontmatter: Dict, body: str) -> str:
        if yaml is None:
            return body
        fm_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{fm_text}\n---\n{body}"

    def _emit_event(self, event_type: str, payload: Dict) -> None:
        """发布事件到总线，失败时降级为日志。"""
        try:
            from core.mnemos_bus import publish_event

            publish_event(event_type, "stress_test", payload)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.debug("压力测试事件发布失败: %s", event_type, exc_info=True)


# ========== 便捷函数 ==========


def stress_test_page(
    page_path: str,
    *,
    wiki_base: str | None = None,
    dry_run: bool = False,
) -> str:
    """便捷函数：测试单个页面，支持 wiki 相对路径。"""
    engine = StressTestEngine(wiki_base=wiki_base, dry_run=dry_run)
    resolved_path = Path(page_path)
    if wiki_base and not resolved_path.is_absolute():
        resolved_path = Path(wiki_base) / resolved_path
    result = engine.test_page(resolved_path)
    return engine.generate_report(result)
