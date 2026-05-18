"""
Knowledge Falsifiability Marking - 知识可证伪性标记

基于波普尔证伪主义，为知识建立主动证伪机制：
1. 每个知识明确声明"什么证据会证伪它"
2. 主动扫描环境，寻找证伪证据
3. 发现证伪时触发知识更新，不是删除而是进化
4. 记录知识韧性 = 经过多少次证伪尝试仍成立

设计原则：
- 可证伪性是科学性的核心标准
- 主动寻找反例，而非被动等待
- 证伪触发修正流程，不是简单删除
- 与 Stress Test、Immune System、Shadow Page 联动
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


class FalsifiabilityStatus(Enum):
    ACTIVE = "active"           # 活跃，尚未被证伪
    CHALLENGED = "challenged"   # 受到挑战，待验证
    FALSIFIED = "falsified"     # 已被证伪，待修正
    REVISED = "revised"         # 已被修正，重新验证中
    DEPRECATED = "deprecated"   # 多次被证伪，建议废弃


@dataclass
class FalsifiabilityMark:
    """可证伪性标记"""
    page_path: str
    page_title: str = ""
    falsifiable_statements: List[str] = field(default_factory=list)  # 会证伪该知识的条件
    test_methods: List[str] = field(default_factory=list)            # 如何测试
    last_tested: str = ""
    falsification_count: int = 0     # 被证伪次数
    verification_count: int = 0      # 验证通过次数
    challenge_count: int = 0         # 受到挑战次数
    status: str = "active"
    updated_at: str = ""


@dataclass
class FalsificationEvidence:
    """证伪证据"""
    page_path: str
    evidence_type: str           # shadow_search / version_change / contradiction / user_report
    evidence_source: str         # 证据来源
    evidence_content: str        # 证据内容
    challenged_statement: str    # 被挑战的声明
    confidence: float = 0.0      # 证伪置信度
    timestamp: str = ""


class FalsifiabilityMarker:
    """知识可证伪性标记器"""

    # 按知识类型推荐的可证伪声明模板
    FALSIFIABILITY_TEMPLATES = {
        "问题-解决": [
            "如果 {tool} 版本 >= X.Y 后该问题不再出现，则解决方案可能已过时",
            "如果在 {scenario} 之外的场景中复现了相同问题但方案无效",
            "如果有更简洁的解决方案出现且效果一致",
        ],
        "经验法则": [
            "如果有大型项目/团队明确违反此经验但仍取得成功",
            "如果在新版本框架/语言中该经验导致性能下降",
            "如果有 A/B 测试数据证明相反做法效果更好",
        ],
        "决策记录": [
            "如果当初决策的假设条件（预算/时间/人力）发生变化",
            "如果出现新技术使得原决策的权衡不再成立",
            "如果事后复盘证明该决策导致了预期之外的负面结果",
        ],
        "反模式": [
            "如果在特定极端约束下（时间/资源极度有限），该反模式成为最优解",
            "如果框架/语言更新后，原反模式被官方推荐",
        ],
        "方法论": [
            "如果在缩短 50% 时间的情况下，完整方法论仍能取得同样效果",
            "如果跳过其中关键步骤仍能得到正确结果",
            "如果有对照实验证明不遵循该方法论效果更好",
        ],
        "洞察关联": [
            "如果有统计研究证明两者无显著相关性",
            "如果在 {scenario} 之外的领域找不到相同关联",
        ],
    }

    # 证伪证据的自动检测规则
    EVIDENCE_PATTERNS = {
        "version_outdated": [
            r"(?:deprecated|removed|no longer supported|已弃用|已移除)",
            r"(?:breaking change|不兼容|重大变更)",
        ],
        "better_solution": [
            r"(?:better|recommended|preferred|更优|推荐|首选)",
            r"(?:since .+? you can|从.+?版本开始|新版支持)",
        ],
        "contradiction": [
            r"(?:however|but|相反|但是|不过)",
            r"(?:not recommended|avoid|不建议|避免)",
        ],
    }

    def __init__(self, wiki_base: str = None, db_path: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.db_path = Path(db_path) if db_path else (
            self.wiki_base / ".kg" / "falsifiability.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        schema = """
        CREATE TABLE IF NOT EXISTS falsifiability_marks (
            page_path TEXT PRIMARY KEY,
            page_title TEXT,
            falsifiable_statements TEXT,  -- JSON array
            test_methods TEXT,             -- JSON array
            last_tested TEXT,
            falsification_count INTEGER DEFAULT 0,
            verification_count INTEGER DEFAULT 0,
            challenge_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS falsification_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_path TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            evidence_source TEXT,
            evidence_content TEXT,
            challenged_statement TEXT,
            confidence REAL,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_page ON falsification_evidence(page_path);
        CREATE INDEX IF NOT EXISTS idx_evidence_time ON falsification_evidence(timestamp);
        CREATE INDEX IF NOT EXISTS idx_mark_status ON falsifiability_marks(status);
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(schema)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 初始化标记 ==========

    def init_mark_for_page(self, page_path: Path) -> Optional[FalsifiabilityMark]:
        """
        为知识页面初始化可证伪性标记

        优先读取页面 frontmatter 中的 falsifiable 字段，
        否则根据知识类型自动生成模板。
        """
        if not page_path.exists():
            return None

        try:
            content = page_path.read_text(encoding="utf-8")
        except Exception:
            return None

        fm = self._extract_frontmatter(content)
        form = fm.get("类型", "")
        tools = self._get_keywords(fm, "工具实体")
        scenarios = self._get_keywords(fm, "场景标签")

        tool_str = tools[0] if tools else "相关工具"
        scenario_str = scenarios[0] if scenarios else "当前场景"

        # 1. 检查页面是否已有自定义 falsifiable 声明
        custom_statements = self._extract_falsifiable_statements(content)

        # 2. 没有自定义声明则使用模板
        if not custom_statements and form in self.FALSIFIABILITY_TEMPLATES:
            custom_statements = [
                t.format(tool=tool_str, scenario=scenario_str)
                for t in self.FALSIFIABILITY_TEMPLATES[form]
            ]

        if not custom_statements:
            return None

        mark = FalsifiabilityMark(
            page_path=str(page_path),
            page_title=fm.get("标题", page_path.stem),
            falsifiable_statements=custom_statements,
            test_methods=self._generate_test_methods(custom_statements, fm),
            status="active",
            updated_at=datetime.now().isoformat()[:19],
        )

        self._save_mark(mark)
        return mark

    def _extract_falsifiable_statements(self, content: str) -> List[str]:
        """从页面内容提取可证伪声明"""
        statements = []

        # 匹配 "可证伪条件" / "Falsifiable if" / "什么会证明这是错的" 段落
        patterns = [
            r"[\-\*]\s*(?:可证伪|什么会证伪|反例条件|失效条件)[：:]\s*(.+?)(?:\n[\-\*]|\n#{1,3}\s|\Z)",
            r"[\-\*]\s*(?:Falsifiable if|Counter-example|When this fails)[：:]\s*(.+?)(?:\n[\-\*]|\n#{1,3}\s|\Z)",
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
                stmt = match.group(1).strip().replace("\n", " ")
                if stmt:
                    statements.append(stmt)

        # 也检查 frontmatter 中的 falsifiable 字段
        fm = self._extract_frontmatter(content)
        if fm and "可证伪" in fm:
            val = fm["可证伪"]
            if isinstance(val, list):
                statements.extend(val)
            elif isinstance(val, str):
                statements.append(val)

        return statements

    def _generate_test_methods(self, statements: List[str], fm: Dict) -> List[str]:
        """为可证伪声明生成测试方法"""
        methods = []
        tool = self._get_keywords(fm, "工具实体")
        tool_str = tool[0] if tool else "对应工具"

        for stmt in statements:
            if "版本" in stmt:
                methods.append(f"检查 {tool_str} 官方 changelog 和 release notes")
            elif "场景" in stmt or "环境" in stmt:
                methods.append(f"在其他场景中尝试复现并验证")
            elif "A/B" in stmt or "测试" in stmt:
                methods.append(f"搜索相关 benchmark 或实验数据")
            elif "大型项目" in stmt or "团队" in stmt:
                methods.append(f"搜索业界案例和工程实践报告")
            else:
                methods.append(f"定期搜索 {tool_str} 最新实践和官方文档")

        return methods or ["定期搜索最新文档和社区讨论"]

    # ========== 扫描与检测 ==========

    def scan_all_marks(self, days_since_last_test: int = 30) -> List[FalsifiabilityMark]:
        """
        扫描所有标记，找出需要重新测试的

        Args:
            days_since_last_test: 超过 N 天未测试的标记

        Returns:
            需要测试的标记列表
        """
        threshold = (datetime.now() - timedelta(days=days_since_last_test)).isoformat()[:19]

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM falsifiability_marks
                   WHERE last_tested < ? OR last_tested = ''
                   AND status IN ('active', 'revised')
                   ORDER BY last_tested""",
                (threshold,)
            ).fetchall()

        return [self._row_to_mark(row) for row in rows]

    def check_shadow_evidence(self, page_path: str,
                              search_results: List[Dict]) -> List[FalsificationEvidence]:
        """
        检查 Shadow Page 搜索结果中是否存在证伪证据

        Args:
            page_path: 知识页面路径
            search_results: shadow page 搜索结果

        Returns:
            发现的证伪证据列表
        """
        evidences = []
        mark = self.get_mark(page_path)
        if not mark:
            return evidences

        for result in search_results:
            content = result.get("content", "") + " " + result.get("title", "")
            source = result.get("url", result.get("source", "unknown"))

            for stmt in mark.falsifiable_statements:
                # 检查内容中是否有证伪信号
                evidence = self._detect_falsification_signal(
                    page_path, stmt, content, source
                )
                if evidence:
                    evidences.append(evidence)

        return evidences

    def _detect_falsification_signal(self, page_path: str,
                                     statement: str,
                                     content: str,
                                     source: str) -> Optional[FalsificationEvidence]:
        """检测内容中是否存在对某声明的证伪信号"""
        content_lower = content.lower()

        # 1. 版本过时信号
        for pattern in self.EVIDENCE_PATTERNS["version_outdated"]:
            if re.search(pattern, content, re.IGNORECASE):
                return FalsificationEvidence(
                    page_path=page_path,
                    evidence_type="version_outdated",
                    evidence_source=source,
                    evidence_content=content[:300],
                    challenged_statement=statement,
                    confidence=0.7,
                    timestamp=datetime.now().isoformat()[:19],
                )

        # 2. 更优方案信号
        for pattern in self.EVIDENCE_PATTERNS["better_solution"]:
            if re.search(pattern, content, re.IGNORECASE):
                return FalsificationEvidence(
                    page_path=page_path,
                    evidence_type="better_solution",
                    evidence_source=source,
                    evidence_content=content[:300],
                    challenged_statement=statement,
                    confidence=0.6,
                    timestamp=datetime.now().isoformat()[:19],
                )

        # 3. 矛盾信号（需要上下文更精确匹配）
        # 提取声明中的关键词进行反向匹配
        keywords = self._extract_keywords_from_statement(statement)
        if keywords and any(kw in content_lower for kw in keywords):
            for pattern in self.EVIDENCE_PATTERNS["contradiction"]:
                if re.search(pattern, content, re.IGNORECASE):
                    return FalsificationEvidence(
                        page_path=page_path,
                        evidence_type="contradiction",
                        evidence_source=source,
                        evidence_content=content[:300],
                        challenged_statement=statement,
                        confidence=0.5,
                        timestamp=datetime.now().isoformat()[:19],
                    )

        return None

    def _extract_keywords_from_statement(self, statement: str) -> List[str]:
        """从可证伪声明中提取关键词"""
        # 简单的中文分词：提取 2-4 字的词
        words = []
        for i in range(len(statement) - 1):
            for length in [4, 3, 2]:
                if i + length <= len(statement):
                    substr = statement[i:i + length]
                    if any(c.isalnum() or '一' <= c <= '鿿' for c in substr):
                        words.append(substr.lower())
        # 去重并限制数量
        seen = set()
        unique = []
        for w in words:
            if w not in seen and len(w) >= 2:
                seen.add(w)
                unique.append(w)
        return unique[:5]

    # ========== 记录与管理 ==========

    def record_verification(self, page_path: str, passed: bool,
                            evidence: FalsificationEvidence = None) -> bool:
        """
        记录验证结果

        Args:
            page_path: 知识页面
            passed: 是否通过验证（True = 未被证伪）
            evidence: 如有证伪证据则传入
        """
        mark = self.get_mark(page_path)
        if not mark:
            return False

        now = datetime.now().isoformat()[:19]

        if passed:
            mark.verification_count += 1
            mark.status = "active"
        else:
            mark.falsification_count += 1
            mark.challenge_count += 1
            if mark.falsification_count >= 3:
                mark.status = "deprecated"
            else:
                mark.status = "falsified"

            if evidence:
                self._save_evidence(evidence)

        mark.last_tested = now
        mark.updated_at = now
        self._save_mark(mark)
        return True

    def record_challenge(self, page_path: str,
                         challenged_statement: str,
                         evidence_content: str = "") -> bool:
        """记录知识受到挑战（用户主动报告）"""
        mark = self.get_mark(page_path)
        if not mark:
            return False

        mark.challenge_count += 1
        mark.status = "challenged"
        mark.updated_at = datetime.now().isoformat()[:19]
        self._save_mark(mark)

        # 保存证据
        evidence = FalsificationEvidence(
            page_path=page_path,
            evidence_type="user_report",
            evidence_source="user",
            evidence_content=evidence_content,
            challenged_statement=challenged_statement,
            confidence=0.8,
            timestamp=datetime.now().isoformat()[:19],
        )
        self._save_evidence(evidence)
        return True

    def revise_mark(self, page_path: str,
                    new_statements: List[str]) -> bool:
        """
        知识被修正后更新可证伪声明

        通常在知识页面内容更新后调用。
        """
        mark = self.get_mark(page_path)
        if not mark:
            return False

        mark.falsifiable_statements = new_statements
        mark.test_methods = self._generate_test_methods(new_statements, {})
        mark.status = "revised"
        mark.updated_at = datetime.now().isoformat()[:19]
        self._save_mark(mark)
        return True

    # ========== 查询 ==========

    def get_mark(self, page_path: str) -> Optional[FalsifiabilityMark]:
        """获取页面的可证伪性标记"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM falsifiability_marks WHERE page_path=?",
                (page_path,)
            ).fetchone()

        if row:
            return self._row_to_mark(row)
        return None

    def get_vulnerable_knowledge(self, min_confidence: float = 0.5) -> List[Dict]:
        """获取易受攻击的知识（高证伪风险）"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, COUNT(e.id) as evidence_count
                   FROM falsifiability_marks m
                   LEFT JOIN falsification_evidence e ON m.page_path = e.page_path
                   WHERE m.status IN ('challenged', 'falsified')
                   GROUP BY m.page_path
                   ORDER BY m.falsification_count DESC, evidence_count DESC"""
            ).fetchall()

        return [
            {
                "page_path": r["page_path"],
                "page_title": r["page_title"],
                "status": r["status"],
                "falsification_count": r["falsification_count"],
                "challenge_count": r["challenge_count"],
                "evidence_count": r["evidence_count"],
            }
            for r in rows
        ]

    def get_resilience_leaderboard(self, top_n: int = 10) -> List[Dict]:
        """
        知识韧性排行榜

        韧性 = 验证通过次数 / (验证通过次数 + 被证伪次数 + 挑战次数)
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM falsifiability_marks ORDER BY verification_count DESC"
            ).fetchall()

        results = []
        for r in rows:
            v = r["verification_count"] or 0
            f = r["falsification_count"] or 0
            c = r["challenge_count"] or 0
            total = v + f + c
            resilience = v / max(total, 1)
            results.append({
                "page_path": r["page_path"],
                "page_title": r["page_title"],
                "resilience": round(resilience, 2),
                "verified": v,
                "falsified": f,
                "challenged": c,
            })

        results.sort(key=lambda x: x["resilience"], reverse=True)
        return results[:top_n]

    # ========== 报告 ==========

    def generate_report(self, page_path: str = None) -> str:
        """生成可证伪性报告"""
        if page_path:
            return self._generate_page_report(page_path)
        return self._generate_global_report()

    def _generate_page_report(self, page_path: str) -> str:
        """单页面报告"""
        mark = self.get_mark(page_path)
        if not mark:
            return f"页面 {Path(page_path).name} 暂无可证伪性标记。"

        # 获取证据
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM falsification_evidence WHERE page_path=? ORDER BY timestamp DESC",
                (page_path,)
            ).fetchall()
        evidences = [self._row_to_evidence(r) for r in rows]

        total_checks = mark.verification_count + mark.falsification_count
        survival_rate = mark.verification_count / max(total_checks, 1)

        lines = [
            f"# 可证伪性报告: {mark.page_title or Path(page_path).name}",
            f"状态: **{self._status_emoji(mark.status)} {mark.status}**",
            f"验证次数: {mark.verification_count} | 证伪次数: {mark.falsification_count} | 挑战次数: {mark.challenge_count}",
            f"生存率: **{survival_rate:.0%}**",
            f"上次测试: {mark.last_tested or '未测试'}",
            "",
            "## 可证伪声明",
            "",
        ]

        for i, stmt in enumerate(mark.falsifiable_statements, 1):
            lines.append(f"{i}. {stmt}")

        if mark.test_methods:
            lines.extend(["", "## 测试方法", ""])
            for i, method in enumerate(mark.test_methods, 1):
                lines.append(f"{i}. {method}")

        if evidences:
            lines.extend(["", "## 证伪证据", ""])
            for e in evidences[:5]:
                lines.append(f"- **[{e.evidence_type}]** {e.challenged_statement[:50]}...")
                lines.append(f"  来源: {e.evidence_source}")
                lines.append(f"  置信度: {e.confidence}")
                lines.append("")

        return "\n".join(lines)

    def _generate_global_report(self) -> str:
        """全局报告"""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM falsifiability_marks").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM falsifiability_marks WHERE status='active'"
            ).fetchone()[0]
            falsified = conn.execute(
                "SELECT COUNT(*) FROM falsifiability_marks WHERE status='falsified'"
            ).fetchone()[0]
            deprecated = conn.execute(
                "SELECT COUNT(*) FROM falsifiability_marks WHERE status='deprecated'"
            ).fetchone()[0]

        leaderboard = self.get_resilience_leaderboard(5)
        vulnerable = self.get_vulnerable_knowledge()

        lines = [
            "# 知识可证伪性全局报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d')}",
            "",
            f"标记总数: **{total}**",
            f"- 活跃: {active}",
            f"- 已被证伪: {falsified}",
            f"- 建议废弃: {deprecated}",
            "",
        ]

        if leaderboard:
            lines.extend(["## 韧性排行榜 TOP5", ""])
            for i, item in enumerate(leaderboard, 1):
                lines.append(f"{i}. **{item['page_title']}** — 韧性 {item['resilience']:.0%} "
                           f"(验证 {item['verified']} / 证伪 {item['falsified']})")
            lines.append("")

        if vulnerable:
            lines.extend(["## 需要关注的知识", ""])
            for item in vulnerable[:5]:
                lines.append(f"- **{item['page_title']}** ({item['status']}) — "
                           f"{item['falsification_count']} 次证伪, {item['evidence_count']} 条证据")
            lines.append("")

        return "\n".join(lines)

    # ========== 辅助方法 ==========

    def _save_mark(self, mark: FalsifiabilityMark):
        """保存标记到数据库"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO falsifiability_marks
                   (page_path, page_title, falsifiable_statements, test_methods,
                    last_tested, falsification_count, verification_count, challenge_count,
                    status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mark.page_path,
                    mark.page_title,
                    json.dumps(mark.falsifiable_statements, ensure_ascii=False),
                    json.dumps(mark.test_methods, ensure_ascii=False),
                    mark.last_tested,
                    mark.falsification_count,
                    mark.verification_count,
                    mark.challenge_count,
                    mark.status,
                    mark.updated_at,
                )
            )
            conn.commit()

    def _save_evidence(self, evidence: FalsificationEvidence):
        """保存证伪证据"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO falsification_evidence
                   (page_path, evidence_type, evidence_source, evidence_content,
                    challenged_statement, confidence, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence.page_path,
                    evidence.evidence_type,
                    evidence.evidence_source,
                    evidence.evidence_content,
                    evidence.challenged_statement,
                    evidence.confidence,
                    evidence.timestamp,
                )
            )
            conn.commit()

    def _row_to_mark(self, row: sqlite3.Row) -> FalsifiabilityMark:
        return FalsifiabilityMark(
            page_path=row["page_path"],
            page_title=row["page_title"] or "",
            falsifiable_statements=json.loads(row["falsifiable_statements"] or "[]"),
            test_methods=json.loads(row["test_methods"] or "[]"),
            last_tested=row["last_tested"] or "",
            falsification_count=row["falsification_count"] or 0,
            verification_count=row["verification_count"] or 0,
            challenge_count=row["challenge_count"] or 0,
            status=row["status"] or "active",
            updated_at=row["updated_at"] or "",
        )

    def _row_to_evidence(self, row: sqlite3.Row) -> FalsificationEvidence:
        return FalsificationEvidence(
            page_path=row["page_path"],
            evidence_type=row["evidence_type"],
            evidence_source=row["evidence_source"] or "",
            evidence_content=row["evidence_content"] or "",
            challenged_statement=row["challenged_statement"] or "",
            confidence=row["confidence"] or 0.0,
            timestamp=row["timestamp"] or "",
        )

    @staticmethod
    def _status_emoji(status: str) -> str:
        return {
            "active": "✅",
            "challenged": "⚠️",
            "falsified": "❌",
            "revised": "🔄",
            "deprecated": "🗑️",
        }.get(status, "❓")

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        import yaml
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
        return {}

    @staticmethod
    def _get_keywords(frontmatter: Dict, layer: str) -> List[str]:
        keywords = frontmatter.get("关键词", {})
        if isinstance(keywords, dict):
            return keywords.get(layer, []) or []
        return []


# ========== 便捷函数 ==========

def init_falsifiability(page_path: str) -> Optional[FalsifiabilityMark]:
    """便捷函数：为页面初始化可证伪性标记"""
    marker = FalsifiabilityMarker()
    return marker.init_mark_for_page(Path(page_path))


def check_knowledge_survival(page_path: str) -> str:
    """便捷函数：检查知识生存状态"""
    marker = FalsifiabilityMarker()
    return marker.generate_report(page_path)
