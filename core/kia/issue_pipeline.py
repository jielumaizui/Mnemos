# -*- coding: utf-8 -*-
"""
Issue Pipeline — 问题处理流水线

统一熵减引擎 + 知识免疫监测到问题后的处理流程。
- IssueRegistry: 问题注册、去重、状态追踪
- AutoFixExecutor: 低风险自动修复（备份 + 执行 + 日志）

设计原则：
- 自动处理 → 低风险、不可逆性低
- 人工确认 → 高风险、不可逆性高
- 忽略标记 → 用户明确不处理
- 追踪闭环 → 每个问题从发现到解决全程可追踪
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import get_config
from core.cognitive.state_contract import sha256_json
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text

logger = logging.getLogger(__name__)

ISSUE_PIPELINE_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:issue-pipeline-markdown",
    contract_revision_id="mnemos.issue_pipeline_markdown.v1",
    contract_text=(
        "IssuePipeline may write only the exact Wiki link or dispute page selected "
        "for one exact registered issue and its reviewed remediation facts."
    ),
    source_namespace="issue-pipeline-markdown",
    producer="issue-pipeline",
    producer_code_hash=sha256_json(
        {
            "module": "core.kia.issue_pipeline",
            "producers": ["AutoFixExecutor._add_wiki_link", "DisputePageGenerator.generate"],
            "version": "mnemos.issue_pipeline_markdown.v1",
        }
    ),
    evaluator_id="issue-pipeline-markdown-evaluator",
    constraints=(
        "Issue identity, type, target, related page, preimage, and output remain exact.",
        "The mutation may not resolve or suppress an issue beyond the selected action.",
    ),
    approved_candidate_key="apply_exact_issue_markdown_action",
    approved_candidate_summary="Apply the exact reviewed issue-pipeline Markdown action.",
    rejected_candidate_key="retain_issue_markdown_state",
    rejected_candidate_summary="Retain the Wiki state when issue facts or bytes drift.",
    approved_reason_code="issue_markdown_binding_verified",
    rejected_reason_code="issue_markdown_binding_rejected",
    committed_metric="issue_markdown_action_committed",
    rejected_metric="unbound_issue_markdown_action_count",
)


def _issue_decision_facts(issue: "Issue") -> dict[str, object]:
    return {
        "schema_version": "mnemos.issue_pipeline_decision_facts.v1",
        "issue_id": issue.issue_id,
        "source_module": issue.source_module,
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "status": issue.status,
        "page_path": issue.page_path,
        "related_pages": list(issue.related_pages),
        "description": issue.description,
        "suggestion": issue.suggestion,
    }


# ========== 数据类 ==========


@dataclass
class Issue:
    """知识问题"""

    issue_id: str = ""  # issue-{hash}
    source_module: str = ""  # "entropy" / "immune"
    issue_type: str = ""  # "delete_duplicate" / "conflict" / "outdated" ...
    severity: str = "low"  # critical / high / medium / low / info
    status: str = "detected"  # detected / auto_fixed / pending / resolved / ignored
    page_path: str = ""  # 主页面路径
    related_pages: List[str] = field(default_factory=list)
    description: str = ""
    suggestion: str = ""
    detected_at: str = ""
    resolved_at: str = ""
    resolved_by: str = ""  # "auto" / "user" / "system"
    resolution_action: str = ""
    resolution_notes: str = ""
    ignore_rule_id: str = ""

    @property
    def page_key(self) -> str:
        """生成去重用的页面键"""
        pages = sorted([self.page_path] + self.related_pages)
        return "|".join(pages)

    def to_dict(self) -> Dict:
        return {
            "issue_id": self.issue_id,
            "source_module": self.source_module,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "status": self.status,
            "page_path": self.page_path,
            "related_pages": json.dumps(self.related_pages, ensure_ascii=False),
            "description": self.description,
            "suggestion": self.suggestion,
            "detected_at": self.detected_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "resolution_action": self.resolution_action,
            "resolution_notes": self.resolution_notes,
            "ignore_rule_id": self.ignore_rule_id,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Issue":
        return cls(
            issue_id=row["issue_id"],
            source_module=row["source_module"],
            issue_type=row["issue_type"],
            severity=row["severity"],
            status=row["status"],
            page_path=row["page_path"] or "",
            related_pages=json.loads(row["related_pages"] or "[]"),
            description=row["description"] or "",
            suggestion=row["suggestion"] or "",
            detected_at=row["detected_at"] or "",
            resolved_at=row["resolved_at"] or "",
            resolved_by=row["resolved_by"] or "",
            resolution_action=row["resolution_action"] or "",
            resolution_notes=row["resolution_notes"] or "",
            ignore_rule_id=row["ignore_rule_id"] or "",
        )


@dataclass
class FixResult:
    """自动修复结果"""

    success: bool = False
    skipped: bool = False
    reason: str = ""
    backup_id: str = ""
    action: str = ""
    error: str = ""


@dataclass
class IgnoreRule:
    """忽略规则"""

    rule_id: str = ""
    issue_type: str = ""
    page_pattern: str = ""
    reason: str = ""
    expires_at: str = ""  # ISO 格式或空字符串表示永久
    created_by: str = "user"
    created_at: str = ""


# ========== 自动修复白名单 ==========

AUTO_FIX_RULES = {
    "entropy.link_related": {
        "action": "add_relation",
        "description": "建立双向链接关系",
        "risk": "low",
    },
    "entropy.cross_reference": {
        "action": "add_wiki_link",
        "description": "在页面末尾添加 [[相关页面]] 引用",
        "risk": "low",
    },
    "immune.orphan": {
        "action": "discover_relations",
        "description": "自动发现可建立的关系",
        "risk": "low",
    },
}


# ========== IssueRegistry ==========

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_issues (
    issue_id TEXT PRIMARY KEY,
    source_module TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    page_path TEXT,
    related_pages TEXT,            -- JSON array
    description TEXT,
    suggestion TEXT,
    detected_at TIMESTAMP,
    resolved_at TIMESTAMP,
    resolved_by TEXT,
    resolution_action TEXT,
    resolution_notes TEXT,
    ignore_rule_id TEXT,
    UNIQUE(source_module, issue_type, page_path, related_pages)
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON knowledge_issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_severity ON knowledge_issues(severity);
CREATE INDEX IF NOT EXISTS idx_issues_page ON knowledge_issues(page_path);
CREATE INDEX IF NOT EXISTS idx_issues_detected ON knowledge_issues(detected_at);

CREATE TABLE IF NOT EXISTS auto_fix_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    page_path TEXT,
    action TEXT,
    success BOOLEAN,
    backup_id TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fix_log_issue ON auto_fix_log(issue_id);

CREATE TABLE IF NOT EXISTS issue_ignore_rules (
    rule_id TEXT PRIMARY KEY,
    issue_type TEXT NOT NULL,
    page_pattern TEXT,
    reason TEXT,
    expires_at TIMESTAMP,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ignore_type ON issue_ignore_rules(issue_type);
"""


class IssueRegistry:
    """问题注册中心

    统一管理熵减引擎和知识免疫发现的问题。
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = (
            Path(db_path) if db_path else (get_config().database_dir / "issue_pipeline.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.executescript(DB_SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row  # noqa
        return conn

    def register(self, issue: Issue) -> Tuple[str, bool]:
        """
        注册问题，自动去重。

        Returns:
            (issue_id, is_new)
        """
        issue.issue_id = self._generate_issue_id(issue)
        issue.detected_at = issue.detected_at or datetime.now(timezone.utc).isoformat()[:19]

        # 检查忽略规则
        if self.is_ignored(issue):
            logger.debug("问题被忽略规则过滤: %s", issue.issue_id)
            return issue.issue_id, False

        with self._conn() as conn:
            # 检查是否已存在
            row = conn.execute(
                "SELECT status, resolved_at FROM knowledge_issues WHERE issue_id = ?",
                (issue.issue_id,),
            ).fetchone()

            if row:
                old_status = row["status"]
                # 已解决的问题再次检测到 → 重新打开（regression）
                if old_status in ("resolved", "ignored"):
                    conn.execute(
                        """UPDATE knowledge_issues
                           SET status = 'detected', detected_at = ?
                           WHERE issue_id = ?""",
                        (issue.detected_at, issue.issue_id),
                    )
                    conn.commit()
                    logger.info("问题回归，重新打开: %s", issue.issue_id)
                    return issue.issue_id, True
                # 已存在且未解决 → 更新时间
                conn.execute(
                    "UPDATE knowledge_issues SET detected_at = ? WHERE issue_id = ?",
                    (issue.detected_at, issue.issue_id),
                )
                conn.commit()
                return issue.issue_id, False

            # 新插入
            d = issue.to_dict()
            conn.execute(
                """INSERT INTO knowledge_issues
                   (issue_id, source_module, issue_type, severity, status,
                    page_path, related_pages, description, suggestion,
                    detected_at, resolved_at, resolved_by, resolution_action,
                    resolution_notes, ignore_rule_id)
                   VALUES (:issue_id, :source_module, :issue_type, :severity, :status,
                           :page_path, :related_pages, :description, :suggestion,
                           :detected_at, :resolved_at, :resolved_by, :resolution_action,
                           :resolution_notes, :ignore_rule_id)""",
                d,
            )
            conn.commit()
            logger.info("新问题注册: %s [%s] %s", issue.issue_id, issue.severity, issue.issue_type)
            return issue.issue_id, True

    def update_status(
        self,
        issue_id: str,
        status: str,
        resolved_by: str = "",
        resolution_action: str = "",
        resolution_notes: str = "",
    ) -> bool:
        """更新问题状态"""
        resolved_at = ""
        if status in ("resolved", "auto_fixed", "ignored"):
            resolved_at = datetime.now(timezone.utc).isoformat()[:19]

        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE knowledge_issues
                   SET status = ?, resolved_by = ?, resolution_action = ?,
                       resolution_notes = ?, resolved_at = ?
                   WHERE issue_id = ?""",
                (status, resolved_by, resolution_action, resolution_notes, resolved_at, issue_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_issue(self, issue_id: str) -> Optional[Issue]:
        """获取单个问题"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_issues WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
            return Issue.from_row(row) if row else None

    def list_issues(
        self,
        status: str | None = None,
        severity: str | None = None,
        page_path: str | None = None,
        source_module: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Issue]:
        """查询问题列表"""
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        if page_path:
            conditions.append("(page_path = ? OR related_pages LIKE ?)")
            params.extend([page_path, f'%"{page_path}"%'])
        if source_module:
            conditions.append("source_module = ?")
            params.append(source_module)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""SELECT * FROM knowledge_issues {where}
                    ORDER BY
                        CASE severity
                            WHEN 'critical' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'medium' THEN 3
                            WHEN 'low' THEN 4
                            ELSE 5
                        END,
                        detected_at DESC
                    LIMIT ? OFFSET ?"""  # nosec B608
        params.extend([limit, offset])  # type: ignore[list-item]

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [Issue.from_row(r) for r in rows]

    def count_by_status(self) -> Dict[str, int]:
        """按状态统计"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM knowledge_issues GROUP BY status"
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    def count_by_severity(self) -> Dict[str, int]:
        """按严重度统计"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT severity, COUNT(*) FROM knowledge_issues GROUP BY severity"
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    def add_ignore_rule(self, rule: IgnoreRule) -> str:
        """添加忽略规则"""
        rule.rule_id = rule.rule_id or f"ignore-{int(time.time() * 1000)}"
        rule.created_at = rule.created_at or datetime.now(timezone.utc).isoformat()[:19]

        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO issue_ignore_rules
                   (rule_id, issue_type, page_pattern, reason, expires_at, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    rule.rule_id,
                    rule.issue_type,
                    rule.page_pattern,
                    rule.reason,
                    rule.expires_at or None,
                    rule.created_by,
                    rule.created_at,
                ),
            )
            conn.commit()
        return rule.rule_id

    def is_ignored(self, issue: Issue) -> bool:
        """检查问题是否被忽略规则命中"""
        with self._conn() as conn:
            # 1. 精确匹配 issue_type + page_path
            rows = conn.execute(
                """SELECT expires_at FROM issue_ignore_rules
                   WHERE issue_type = ? AND (page_pattern = ? OR page_pattern = '*')
                   ORDER BY created_at DESC""",
                (issue.issue_type, issue.page_path),
            ).fetchall()

            # 2. 通用匹配（仅 issue_type）
            if not rows:
                rows = conn.execute(
                    """SELECT expires_at FROM issue_ignore_rules
                       WHERE issue_type = ? AND page_pattern IS NULL
                       ORDER BY created_at DESC""",
                    (issue.issue_type,),
                ).fetchall()

            for row in rows:
                expires = row["expires_at"]
                if expires is None:
                    return True
                try:
                    if datetime.fromisoformat(expires) > datetime.now(timezone.utc):
                        return True
                except ValueError:
                    return True
            return False

    def cleanup_expired_ignores(self) -> int:
        """清理过期的忽略规则"""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM issue_ignore_rules WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            conn.commit()
            return cursor.rowcount

    def _generate_issue_id(self, issue: Issue) -> str:
        """生成问题唯一 ID"""
        pages = sorted([issue.page_path] + issue.related_pages)
        raw = f"{issue.source_module}:{issue.issue_type}:{':'.join(pages)}"
        h = hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
        return f"issue-{h}"


# ========== AutoFixExecutor ==========


class AutoFixExecutor:
    """自动修复执行器

    只执行低风险操作，所有操作记录日志，操作前备份。
    """

    def __init__(
        self,
        registry: IssueRegistry | None = None,
        wiki_base: Path | None = None,
        backup_dir: Path | None = None,
    ):
        self.registry = registry
        self.wiki_base = wiki_base or get_config().wiki_dir
        self.backup_dir = backup_dir or (get_config().data_dir / "backups" / "auto_fix")
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def can_auto_fix(self, issue: Issue) -> bool:
        """判断问题是否可以自动修复"""
        key = f"{issue.source_module}.{issue.issue_type}"
        return key in AUTO_FIX_RULES

    def execute(self, issue: Issue) -> FixResult:
        """
        执行自动修复。

        流程：检查白名单 → 创建备份 → 执行修复 → 记录日志 → 更新状态
        """
        if not self.can_auto_fix(issue):
            return FixResult(skipped=True, reason="not_auto_fixable")

        if self.registry and self.registry.is_ignored(issue):
            return FixResult(skipped=True, reason="issue_ignored")

        backup_id = self._create_backup(issue)

        try:
            action = self._apply_fix(issue)
            self._log_fix(issue, True, backup_id, action)

            if self.registry:
                self.registry.update_status(
                    issue.issue_id,
                    status="auto_fixed",
                    resolved_by="auto",
                    resolution_action=action,
                )

            return FixResult(success=True, backup_id=backup_id, action=action)
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            logger.warning("自动修复失败 %s: %s", issue.issue_id, e)
            self._rollback(backup_id)
            self._log_fix(issue, False, backup_id, "", str(e))
            return FixResult(success=False, backup_id=backup_id, error=str(e))

    def _apply_fix(self, issue: Issue) -> str:
        """执行具体的修复动作"""
        key = f"{issue.source_module}.{issue.issue_type}"
        rule = AUTO_FIX_RULES.get(key)
        if not rule:
            raise ValueError(f"未知自动修复规则: {key}")

        action = rule["action"]

        if action == "add_relation":
            return self._add_relation(issue)
        elif action == "add_wiki_link":
            return self._add_wiki_link(issue)
        elif action == "discover_relations":
            return self._discover_relations(issue)

        raise ValueError(f"不支持的自动修复动作: {action}")

    def _add_relation(self, issue: Issue) -> str:
        """在知识图谱中建立关系"""
        try:
            from core.kia.knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            # related_pages 中应该包含两个页面
            pages = issue.related_pages
            if len(pages) >= 2:
                # 自动发现关系并应用
                rels = kg.discover_relations(Path(pages[0]), [Path(pages[1])])
                added = kg.apply_discovered(rels, min_confidence=0.7)
                return f"add_relation:{added}"
            return "add_relation:0"
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            raise RuntimeError(f"建立关系失败: {e}") from e

    def _add_wiki_link(self, issue: Issue) -> str:
        """在页面末尾添加 [[相关页面]] 引用"""
        target_page = Path(issue.page_path)
        if not issue.related_pages:
            return "add_wiki_link:0"

        ref_page = Path(issue.related_pages[0]).stem

        if not target_page.exists():
            raise FileNotFoundError(f"页面不存在: {target_page}")

        content = target_page.read_text(encoding="utf-8")
        link_line = f"\n\n## 相关页面\n\n- [[{ref_page}]]\n"

        # 避免重复添加
        if f"[[{ref_page}]]" in content:
            return "add_wiki_link:already_exists"

        evidence_refs = [f"issue:{issue.issue_id}"]
        submit_or_write_markdown_with_decision(
            decision_policy=ISSUE_PIPELINE_MARKDOWN_POLICY,
            decision_facts={
                **_issue_decision_facts(issue),
                "selected_action": "add_wiki_link",
                "ref_page": ref_page,
            },
            decision_task=f"Add reviewed Wiki link for issue {issue.issue_id}",
            decision_goal="Apply the exact low-risk link remediation selected for the issue.",
            decision_created_at=datetime.now(timezone.utc).isoformat(),
            wiki_base=self.wiki_base,
            target_path=target_page,
            content=content + link_line,
            source="issue_pipeline",
            actor="system",
            evidence_refs=evidence_refs,
            proposed_action="add_wiki_link",
            expected_existing_hash=sha256_text(content),
            metadata={"ref_page": ref_page, "issue_type": issue.issue_type},
        )
        return f"add_wiki_link:{ref_page}"

    def _discover_relations(self, issue: Issue) -> str:
        """调用知识图谱自动发现关系"""
        try:
            from core.kia.knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            page = Path(issue.page_path)
            if not page.exists():
                return "discover_relations:page_not_found"
            rels = kg.discover_relations(page)
            added = kg.apply_discovered(rels, min_confidence=0.5)
            return f"discover_relations:{added}"
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            raise RuntimeError(f"关系发现失败: {e}") from e

    def _create_backup(self, issue: Issue) -> str:
        """创建备份，返回备份标识"""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_id = f"{issue.issue_id}_{ts}"

        # 备份涉及的页面文件，文件名中保留相对路径以避免同名冲突
        for page in [issue.page_path] + issue.related_pages:
            p = Path(page)
            if p.exists():
                try:
                    rel_part = p.relative_to(self.wiki_base).as_posix().replace("/", "__")
                except ValueError:
                    rel_part = p.name
                backup_file = self.backup_dir / f"{backup_id}_{rel_part}"
                backup_file.write_bytes(p.read_bytes())

        return backup_id

    def _rollback(self, backup_id: str):
        """回滚备份"""
        if not backup_id:
            return
        for backup_file in self.backup_dir.glob(f"{backup_id}_*"):
            try:
                # 从备份文件名中提取原始相对路径（__ 替换回 /）
                encoded_rel = backup_file.name[len(backup_id) + 1 :]
                rel_path = encoded_rel.replace("__", "/")
                page_file = self.wiki_base / rel_path
                if page_file.exists():
                    page_file.write_bytes(backup_file.read_bytes())
                    logger.info("回滚备份: %s", page_file)
                    continue
            except (OSError, UnicodeError, ValueError, TypeError) as e:
                logger.warning("回滚失败 %s: %s", backup_file, e, exc_info=True)

    def _log_fix(self, issue: Issue, success: bool, backup_id: str, action: str, error: str = ""):
        """记录自动修复日志"""
        if not self.registry:
            return
        with self.registry._conn() as conn:
            conn.execute(
                """INSERT INTO auto_fix_log
                   (issue_id, issue_type, page_path, action, success, backup_id, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    issue.issue_id,
                    issue.issue_type,
                    issue.page_path,
                    action,
                    success,
                    backup_id,
                    error,
                ),
            )
            conn.commit()


# ========== 人工确认：争议页面生成器 ==========


class DisputePageGenerator:
    """争议页面生成器

    为需要人工确认的高风险问题生成 Obsidian 争议仲裁页面。
    用户可在页面中编辑裁决结论，系统读取后更新问题状态。
    """

    REPORTS_DIR = "99-Reports"

    def __init__(self, wiki_base: Path | None = None):
        self.wiki_base = wiki_base or get_config().wiki_dir
        self.reports_dir = self.wiki_base / self.REPORTS_DIR
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, issue: Issue) -> Path:
        """
        生成争议仲裁页面。

        Returns:
            生成的页面路径
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"争议-{issue.issue_type}-{ts}.md"
        page_path = self.reports_dir / filename

        lines = self._build_content(issue)
        rendered_content = "\n".join(lines)
        evidence_refs = [f"issue:{issue.issue_id}"]
        submit_or_write_markdown_with_decision(
            decision_policy=ISSUE_PIPELINE_MARKDOWN_POLICY,
            decision_facts={
                **_issue_decision_facts(issue),
                "selected_action": "create_issue_dispute_page",
            },
            decision_task=f"Create dispute page for issue {issue.issue_id}",
            decision_goal="Expose the exact unresolved issue for explicit adjudication.",
            decision_created_at=datetime.now(timezone.utc).isoformat(),
            wiki_base=self.wiki_base,
            target_path=page_path,
            content=rendered_content,
            source="issue_pipeline",
            actor="system",
            evidence_refs=evidence_refs,
            proposed_action="create_issue_dispute_page",
            metadata={"issue_type": issue.issue_type, "severity": issue.severity},
        )

        logger.info("争议页面生成: %s", page_path)
        return page_path

    def _build_content(self, issue: Issue) -> List[str]:
        """构建争议页面内容"""
        title = Path(issue.page_path).stem if issue.page_path else "未知页面"

        lines = [
            "---",
            f"issue_type: {issue.issue_type}",
            f"severity: {issue.severity}",
            "status: pending",
            f"source_module: {issue.source_module}",
            f"detected_at: {issue.detected_at}",
            f"page_path: {issue.page_path}",
            "---",
            "",
            f"# 争议：{title}",
            "",
            "## 问题描述",
            "",
            issue.description or "（无描述）",
            "",
            "## 系统建议",
            "",
            issue.suggestion or "（无建议）",
            "",
            "## 关联页面",
            "",
        ]
        for p in issue.related_pages:
            lines.append(f"- [[{Path(p).stem}]]")
        if not issue.related_pages:
            lines.append("- 无")

        lines.extend(
            [
                "",
                "## 请裁决",
                "",
                "- [ ] 已解决（按系统建议处理）",
                "- [ ] 忽略此问题",
                "- [ ] 需要更多信息",
                "",
                "## 备注",
                "",
                "_（在此记录你的裁决理由）_",
                "",
            ]
        )
        return lines

    def parse_resolution(self, page_path: Path) -> Optional[Dict]:
        """
        从争议页面解析用户裁决。

        Returns:
            {"choice": str, "notes": str} 或 None
        """
        if not page_path.exists():
            return None

        content = page_path.read_text(encoding="utf-8")

        # 解析复选框
        if "- [x] 已解决" in content.lower() or "- [X] 已解决" in content:
            choice = "resolved"
        elif "- [x] 忽略此问题" in content.lower() or "- [X] 忽略此问题" in content:
            choice = "ignored"
        elif "- [x] 需要更多信息" in content.lower() or "- [X] 需要更多信息" in content:
            choice = "needs_info"
        else:
            return None

        # 提取备注
        notes = ""
        if "## 备注" in content:
            parts = content.split("## 备注", 1)
            if len(parts) > 1:
                notes = parts[1].strip().strip("_").strip()

        return {"choice": choice, "notes": notes}


# ========== 便捷函数 ==========


def get_issue_registry(db_path: str | None = None) -> IssueRegistry:
    """获取 IssueRegistry 单例"""
    return IssueRegistry(db_path=db_path)


def get_auto_fix_executor(registry: IssueRegistry | None = None) -> AutoFixExecutor:
    """获取 AutoFixExecutor 单例"""
    if registry is None:
        registry = get_issue_registry()
    return AutoFixExecutor(registry=registry)


def get_dispute_generator(wiki_base: Path | None = None) -> DisputePageGenerator:
    """获取 DisputePageGenerator 单例"""
    return DisputePageGenerator(wiki_base=wiki_base)
