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
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.config import get_config


# ==================== _LazyPath ====================

class _LazyPath:
    """Lazy path that resolves get_config() only on access."""
    __slots__ = ('_base', '_segments')

    def __init__(self, base: str = "data_dir", *segments):
        self._base = base
        self._segments = segments

    def __truediv__(self, other):
        return _LazyPath(self._base, *self._segments, other)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def _resolve(self) -> Path:
        config = get_config()
        if self._base == "data_dir":
            result = config.data_dir
        elif self._base == "wiki_dir":
            result = config.wiki_dir
        else:
            result = config.data_dir
        for seg in self._segments:
            result = result / seg
        return result

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return f"LazyPath({self._base}:{'/'.join(self._segments)})"

    def __fspath__(self):
        return str(self._resolve())

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __hash__(self):
        return hash(self._resolve())

    def __eq__(self, other):
        return self._resolve() == other

    def __iter__(self):
        return iter(self._resolve())


DB_PATH = _LazyPath("data_dir", "stress_test.db")
WIKI_DIR = _LazyPath("wiki_dir")


# ==================== Data Classes ====================

@dataclass
class Challenge:
    """挑战项"""
    challenge_type: str          # boundary / counter_example / extreme / combination / temporal
    question: str                # 挑战问题
    expected_behavior: str = ""  # 知识在此场景下应如何表现
    risk_level: str = "medium"   # low / medium / high
    triggered_by: str = ""       # 触发这个挑战的知识特征


@dataclass
class StressTestResult:
    """压力测试结果"""
    page_path: str
    page_title: str = ""
    resilience_score: float = 0.0   # 0-10，知识韧性评分
    challenges: List[Challenge] = field(default_factory=list)
    passed_challenges: int = 0
    failed_challenges: int = 0
    blind_spots: List[str] = field(default_factory=list)


# ==================== StressTestEngine ====================

class StressTestEngine:
    """知识压力测试引擎"""

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

    def __init__(self, wiki_base: str | None = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            self.wiki_base = WIKI_DIR
        self.inbox = self.wiki_base / "00-Inbox"
        self._db_path = DB_PATH

    # ---- DB helpers ----

    def _get_conn(self) -> sqlite3.Connection:
        db = Path(str(self._db_path))
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), timeout=10)
        conn.row_factory = sqlite3.Row
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
                blind_spots_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_str_page ON stress_test_results(page_path);
        """)
        conn.commit()
        conn.close()

    def save_result(self, result: StressTestResult):
        """保存测试结果到 SQLite"""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO stress_test_results
                   (page_path, page_title, resilience_score,
                    challenges_count, blind_spots_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    result.page_path,
                    result.page_title,
                    result.resilience_score,
                    len(result.challenges),
                    len(result.blind_spots),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ---- Core Logic ----

    def test_page(self, page_path: Path,
                  use_llm: bool = False) -> StressTestResult:
        """
        对单个页面进行压力测试

        Args:
            page_path: 知识页面路径
            use_llm: 是否使用 LLM 生成深度挑战（预留接口）

        Returns:
            StressTestResult
        """
        result = StressTestResult(page_path=str(page_path))

        if not page_path.exists():
            return result

        try:
            content = page_path.read_text(encoding="utf-8")
            fm = self._extract_frontmatter(content)
            body = self._extract_body(content)
        except Exception:
            return result

        result.page_title = self._extract_title(content) or page_path.stem

        # 1. 基于知识类型生成挑战
        form = fm.get("类型", "")
        templates = self.CHALLENGE_TEMPLATES.get(form, [])

        tools = self._get_keywords(fm, "工具实体")
        scenarios = self._get_keywords(fm, "场景标签")
        tool_str = tools[0] if tools else "相关工具"
        scenario_str = scenarios[0] if scenarios else "当前场景"

        for template in templates:
            question = template["template"].format(
                tool=tool_str,
                scenario=scenario_str,
            )
            result.challenges.append(Challenge(
                challenge_type=template["type"],
                question=question,
                risk_level=template.get("risk", "medium"),
                triggered_by=f"知识类型: {form}",
            ))

        # 2. 基于适用边界生成挑战
        boundaries = self._extract_boundaries(body)
        if boundaries.get("applies"):
            result.challenges.append(Challenge(
                challenge_type="boundary",
                question=f"适用条件声明为「{boundaries['applies']}」，但这个声明本身是否完整？是否有遗漏的隐含条件？",
                risk_level="high",
                triggered_by="适用边界",
            ))

        if boundaries.get("not_applies"):
            result.challenges.append(Challenge(
                challenge_type="boundary",
                question=f"不适用场景声明为「{boundaries['not_applies']}」，是否还有其他未声明的不适用场景？",
                risk_level="medium",
                triggered_by="不适用边界",
            ))

        # 3. 基于反模式生成挑战
        anti_patterns = self._extract_anti_patterns(body)
        for anti in anti_patterns[:2]:
            result.challenges.append(Challenge(
                challenge_type="counter_example",
                question=f"反模式「{anti[:50]}...」是否有任何例外情况？",
                risk_level="medium",
                triggered_by="反模式",
            ))

        # 4. 基于时效性生成挑战
        temporal = fm.get("时效性", "")
        version_tag = fm.get("版本标记", "")
        if temporal == "版本绑定" and version_tag:
            result.challenges.append(Challenge(
                challenge_type="temporal",
                question=f"版本标记为「{version_tag}」，该版本是否有已知的弃用计划或重大变更？",
                risk_level="high",
                triggered_by="版本绑定",
            ))
        elif temporal == "上下文相关":
            result.challenges.append(Challenge(
                challenge_type="temporal",
                question="上下文相关知识容易随环境变化而失效，当前上下文是否已发生变化？",
                risk_level="medium",
                triggered_by="上下文相关",
            ))

        # 5. 基于置信度生成挑战
        confidence = float(fm.get("置信度", 0.5))
        if confidence < 0.6:
            result.challenges.append(Challenge(
                challenge_type="boundary",
                question=f"置信度仅 {confidence}，这意味着知识可靠性不足。什么条件下可以提升到 0.8 以上？",
                risk_level="high",
                triggered_by="低置信度",
            ))

        # 计算韧性评分
        result.resilience_score = self._calculate_resilience(result, fm, boundaries)

        # 识别盲区
        result.blind_spots = self._identify_blind_spots(result, fm, boundaries)

        return result

    def batch_test(self, limit: int | None = None) -> List[StressTestResult]:
        """批量测试所有知识页面"""
        results = []
        wiki_base = Path(str(self.wiki_base))
        inbox = wiki_base / "00-Inbox"
        if not inbox.exists():
            return results

        pages = list(inbox.glob("*.md"))
        if limit:
            pages = pages[:limit]

        for page in pages:
            result = self.test_page(page)
            if result.challenges:
                results.append(result)
                self.save_result(result)

        return results

    def _calculate_resilience(self, result: StressTestResult,
                              frontmatter: Dict,
                              boundaries: Dict) -> float:
        """计算知识韧性评分 0-10"""
        score = 5.0  # 基础分

        # 有明确适用边界 +2
        if boundaries.get("applies") and boundaries.get("not_applies"):
            score += 2
        elif boundaries.get("applies") or boundaries.get("not_applies"):
            score += 1

        # 有反模式 +1
        if frontmatter.get("类型") == "反模式" or boundaries.get("anti_patterns"):
            score += 1

        # 置信度高 +1
        confidence = float(frontmatter.get("置信度", 0))
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

        return max(0, min(10, score))

    def _identify_blind_spots(self, result: StressTestResult,
                               frontmatter: Dict,
                               boundaries: Dict) -> List[str]:
        """识别知识盲区"""
        blind_spots = []

        # 缺少适用边界
        if not boundaries.get("applies") and not boundaries.get("not_applies"):
            blind_spots.append("未声明适用边界，可能导致误用")

        # 缺少反模式
        if frontmatter.get("类型") in ["经验法则", "方法论"] and not boundaries.get("anti_patterns"):
            blind_spots.append("缺少反模式/注意事项，未考虑失败场景")

        # 低置信度
        confidence = float(frontmatter.get("置信度", 0.5))
        if confidence < 0.5:
            blind_spots.append("置信度过低，知识基础不牢")

        # 单源证据
        if frontmatter.get("证据级别") == "单源":
            blind_spots.append("单源证据，未经交叉验证")

        # 时效性风险
        if frontmatter.get("时效性") == "版本绑定" and not frontmatter.get("版本标记"):
            blind_spots.append("声明为版本绑定但未标注版本号")

        return blind_spots

    def generate_report(self, result: StressTestResult) -> str:
        """生成压力测试报告 Markdown"""
        lines = [
            f"# 压力测试报告: {result.page_title}",
            f"韧性评分: **{result.resilience_score:.1f}** / 10",
            f"挑战数量: {len(result.challenges)}",
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
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception:
                    pass
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

    @staticmethod
    def _extract_boundaries(body: str) -> Dict:
        """提取适用边界"""
        boundaries = {}

        applies_match = re.search(
            r"[\-\*]\s*适用于[：:]\s*(.+?)(?:\n[\-\*]|\n#{1,3}\s|\Z)",
            body, re.DOTALL
        )
        if applies_match:
            boundaries["applies"] = applies_match.group(1).strip().replace("\n", " ")

        not_applies_match = re.search(
            r"[\-\*]\s*不适用于[：:]\s*(.+?)(?:\n[\-\*]|\n#{1,3}\s|\Z)",
            body, re.DOTALL
        )
        if not_applies_match:
            boundaries["not_applies"] = not_applies_match.group(1).strip().replace("\n", " ")

        return boundaries

    @staticmethod
    def _extract_anti_patterns(body: str) -> List[str]:
        """提取反模式列表"""
        patterns = []
        in_anti_section = False

        for line in body.split("\n"):
            if "反模式" in line or "注意事项" in line:
                in_anti_section = True
                continue
            if in_anti_section and line.startswith("#"):
                break
            if in_anti_section and line.strip().startswith(("-", "*")):
                text = line.strip().lstrip("- *").strip()
                if text:
                    patterns.append(text)

        return patterns


# ========== 便捷函数 ==========

def stress_test_page(page_path: str) -> str:
    """便捷函数：测试单个页面"""
    engine = StressTestEngine()
    result = engine.test_page(Path(page_path))
    return engine.generate_report(result)
