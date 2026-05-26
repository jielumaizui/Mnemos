# -*- coding: utf-8 -*-
"""
DeferredDistill — 延迟与联合蒸馏

场景 A：低分记录进入延迟队列，按实体聚类后批量评估
场景 B：Wiki 页面增量更新，尊重用户编辑区域
场景 C：跨页面联合蒸馏，碎片检测 + 合并

权重适配器层次：HardcodedWeightAdapter → AutoSwitchWeightAdapter → BayesianWeightAdapter
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import log1p
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import get_config
from core.hephaestus.distillation_engine import (
    DistillationEngine, DistillationResult, KnowledgeFragment,
    HostAgentCaller, build_session_text,
)

logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"


# ========== 数据模型 ==========

@dataclass
class DeferredRecord:
    """延迟队列记录"""
    id: Optional[int] = None
    session_id: str = ""
    content: str = ""
    agent_name: str = ""
    score: float = 0.0
    entity_hint: str = ""
    status: str = "pending"  # pending / clustered / distilling / done / skipped
    created_at: str = ""
    cluster_id: Optional[int] = None


@dataclass
class DistillDecision:
    """蒸馏决策"""
    action: str  # distill / skip / defer
    reason: str = ""
    priority: float = 0.0
    cluster_id: Optional[int] = None


# ========== 权重适配器 ==========

# 按领域的硬编码权重
_DOMAIN_WEIGHTS = {
    "tech": 1.2,
    "concept": 1.0,
    "people": 0.6,
    "project": 1.1,
    "moc": 0.8,
}


class HardcodedWeightAdapter:
    """阶段1：硬编码权重"""

    def get_weight(self, domain: str) -> float:
        return _DOMAIN_WEIGHTS.get(domain, 0.8)

    def update(self, domain: str, outcome: float):
        pass  # 硬编码不更新


class BayesianWeightAdapter:
    """阶段2：Beta-二项模型，Thompson 采样"""

    def __init__(self):
        self._priors: Dict[str, Tuple[float, float]] = {}

    def get_weight(self, domain: str) -> float:
        alpha, beta = self._priors.get(domain, (1.0, 1.0))
        import random
        return random.betavariate(alpha, beta)

    def update(self, domain: str, outcome: float):
        alpha, beta = self._priors.get(domain, (1.0, 1.0))
        if outcome > 0.5:
            alpha += outcome
        else:
            beta += (1.0 - outcome)
        self._priors[domain] = (alpha, beta)


class AutoSwitchWeightAdapter:
    """自动切换权重适配器

    某领域积累 30+ 反馈样本时切换到 Bayesian，否则回退 Hardcoded。
    """

    SAMPLE_THRESHOLD = 30

    def __init__(self):
        self._hardcoded = HardcodedWeightAdapter()
        self._bayesian = BayesianWeightAdapter()
        self._sample_counts: Dict[str, int] = {}

    def get_weight(self, domain: str) -> float:
        if self._sample_counts.get(domain, 0) >= self.SAMPLE_THRESHOLD:
            return self._bayesian.get_weight(domain)
        return self._hardcoded.get_weight(domain)

    def update(self, domain: str, outcome: float):
        self._sample_counts[domain] = self._sample_counts.get(domain, 0) + 1
        self._bayesian.update(domain, outcome)
        self._hardcoded.update(domain, outcome)


# ========== OutcomeCollector ==========

class OutcomeCollector:
    """蒸馏结果观察器 — 7 天观察期，5 个质量信号

    信号：页面浏览量、编辑量、反向链接、搜索命中、星标
    """

    OBSERVATION_DAYS = 7

    def daily_collect(self) -> List[Dict]:
        """每日收集蒸馏结果的质量信号"""
        cutoff = (datetime.now() - timedelta(days=self.OBSERVATION_DAYS)).isoformat()
        results = []

        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT session_id, wiki_page_paths FROM sync_log "
                    "WHERE distilled_at > ? AND distill_status = 'done'",
                    (cutoff,),
                )
                for row in cursor:
                    session_id = row[0]
                    paths_json = row[1] or "[]"
                    try:
                        paths = json.loads(paths_json)
                    except json.JSONDecodeError:
                        paths = []

                    signals = self._collect_page_signals(paths)
                    if signals:
                        results.append({
                            "session_id": session_id,
                            "paths": paths,
                            "signals": signals,
                            "outcome": self._compute_outcome(signals),
                        })
        except Exception as e:
            logger.warning(f"OutcomeCollector 采集失败: {e}")

        return results

    def _collect_page_signals(self, paths: List[str]) -> Dict:
        """收集页面级质量信号"""
        signals = {
            "views": 0, "edits": 0, "backlinks": 0,
            "search_hits": 0, "stars": 0,
        }
        for path in paths:
            p = Path(path)
            if not p.exists():
                continue
            try:
                content = p.read_text(encoding="utf-8")
                # 反向链接数
                signals["backlinks"] += len(re.findall(r'\[\[.*?\]\]', content))
                # 编辑标记
                signals["edits"] += content.count("<!-- user-edited -->")
            except Exception:
                continue
        return signals

    def _compute_outcome(self, signals: Dict) -> float:
        """计算蒸馏结果质量分（0-1）"""
        score = 0.2
        if signals["backlinks"] > 0:
            score += min(0.3, signals["backlinks"] * 0.1)
        if signals["edits"] > 0:
            score += min(0.2, signals["edits"] * 0.1)
        if signals["views"] > 3:
            score += 0.15
        if signals["search_hits"] > 0:
            score += 0.15
        return min(1.0, score)


# ========== DeferredDistillationQueue ==========

class DeferredDistillationQueue:
    """延迟蒸馏队列

    低分记录 → 延迟入队 → 按实体聚类 → 四维度聚合评分 → 蒸馏决策
    """

    QUEUE_TABLE = """
        CREATE TABLE IF NOT EXISTS deferred_distill_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            content TEXT,
            agent_name TEXT DEFAULT '',
            score REAL DEFAULT 0,
            entity_hint TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            cluster_id INTEGER
        )
    """

    def __init__(self, weight_adapter=None):
        self._weight_adapter = weight_adapter or AutoSwitchWeightAdapter()
        self._outcome_collector = OutcomeCollector()
        self._init_db()

    def _init_db(self):
        db_path = _get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            conn.execute(self.QUEUE_TABLE)
            conn.commit()

    def enqueue(self, session_id: str, content: str, score: float,
                agent_name: str = "", entity_hint: str = "") -> None:
        """低分记录入队"""
        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                conn.execute(
                    """INSERT INTO deferred_distill_queue
                       (session_id, content, agent_name, score, entity_hint, status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (session_id, content[:5000], agent_name, score, entity_hint,
                     datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"延迟队列入队失败: {e}")

    def evaluate_single(self, record: DeferredRecord) -> DistillDecision:
        """评估单条记录是否值得蒸馏

        四维度聚合评分：count / overlap / time / complement
        """
        score = record.score

        # count 维度：同类记录越多越可能有价值
        count = self._count_same_entity(record.entity_hint)
        count_score = min(1.0, log1p(count) / log1p(10))

        # time 维度：近期记录权重更高
        try:
            created = datetime.fromisoformat(record.created_at)
            age_days = (datetime.now() - created).days
            time_score = max(0.0, 1.0 - age_days / 30.0)
        except Exception:
            time_score = 0.5

        # domain 权重
        domain = self._infer_domain(record.content)
        domain_weight = self._weight_adapter.get_weight(domain)

        # 聚合
        priority = (0.3 * score + 0.3 * count_score + 0.2 * time_score + 0.2 * domain_weight)

        if priority >= 0.5:
            return DistillDecision(action="distill", reason=f"聚合评分 {priority:.2f}", priority=priority)
        elif priority >= 0.3:
            return DistillDecision(action="defer", reason=f"聚合评分不足 {priority:.2f}", priority=priority)
        return DistillDecision(action="skip", reason=f"聚合评分过低 {priority:.2f}", priority=priority)

    def daily_batch(self, engine: DistillationEngine = None) -> Dict:
        """每日批量处理延迟队列

        1. 按实体聚类
        2. 逐条评估
        3. 蒸馏或跳过
        """
        records = self._load_pending()
        if not records:
            return {"total": 0, "distilled": 0, "skipped": 0, "deferred": 0}

        # 按实体聚类
        clusters = self._cluster_by_entity(records)

        stats = {"total": len(records), "distilled": 0, "skipped": 0, "deferred": 0}

        for cluster_id, cluster_records in clusters.items():
            for record in cluster_records:
                record.cluster_id = cluster_id
                decision = self.evaluate_single(record)

                if decision.action == "distill":
                    if engine:
                        self._distill_record(engine, record)
                    stats["distilled"] += 1
                elif decision.action == "skip":
                    self._update_status(record.id, "skipped")
                    stats["skipped"] += 1
                else:
                    stats["deferred"] += 1

        # 收集蒸馏结果观察
        outcomes = self._outcome_collector.daily_collect()
        for outcome in outcomes:
            domain = self._infer_domain(outcome.get("session_id", ""))
            self._weight_adapter.update(domain, outcome.get("outcome", 0.5))

        return stats

    def _load_pending(self) -> List[DeferredRecord]:
        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT id, session_id, content, agent_name, score, "
                    "entity_hint, status, created_at, cluster_id "
                    "FROM deferred_distill_queue WHERE status = 'pending'"
                )
                return [
                    DeferredRecord(
                        id=row[0], session_id=row[1], content=row[2],
                        agent_name=row[3], score=row[4], entity_hint=row[5],
                        status=row[6], created_at=row[7], cluster_id=row[8],
                    )
                    for row in cursor
                ]
        except Exception:
            return []

    def _cluster_by_entity(self, records: List[DeferredRecord]) -> Dict[int, List[DeferredRecord]]:
        """按实体提示聚类"""
        clusters: Dict[int, List[DeferredRecord]] = {}
        entity_map: Dict[str, int] = {}
        next_id = 1

        for record in records:
            entity = record.entity_hint or self._extract_entity(record.content)
            if entity in entity_map:
                cluster_id = entity_map[entity]
            else:
                cluster_id = next_id
                entity_map[entity] = cluster_id
                next_id += 1
            clusters.setdefault(cluster_id, []).append(record)

        return clusters

    def _count_same_entity(self, entity_hint: str) -> int:
        if not entity_hint:
            return 0
        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM deferred_distill_queue "
                    "WHERE entity_hint = ? AND status = 'pending'",
                    (entity_hint,),
                )
                return cursor.fetchone()[0]
        except Exception:
            return 0

    def _distill_record(self, engine: DistillationEngine, record: DeferredRecord):
        """蒸馏单条记录"""
        try:
            messages = [{"role": "assistant", "content": record.content}]
            result = engine.process(record.session_id, messages)
            if result.judgment == "knowledge" and result.fragments:
                engine.write_pages(result)
            self._update_status(record.id, "done")
        except Exception as e:
            logger.warning(f"延迟蒸馏失败 {record.session_id}: {e}")
            self._update_status(record.id, "skipped")

    def _update_status(self, record_id: int, status: str):
        try:
            with sqlite3.connect(str(_get_db_path()), timeout=5) as conn:
                conn.execute(
                    "UPDATE deferred_distill_queue SET status = ? WHERE id = ?",
                    (status, record_id),
                )
                conn.commit()
        except Exception:
            pass

    @staticmethod
    def _extract_entity(content: str) -> str:
        """从内容中提取主实体关键词"""
        words = re.findall(r'[a-zA-Z_]{3,}', content[:500])
        cn_words = re.findall(r'[一-龥]{2,4}', content[:500])
        all_words = words + cn_words
        if not all_words:
            return "unknown"
        from collections import Counter
        counter = Counter(all_words)
        return counter.most_common(1)[0][0]

    @staticmethod
    def _infer_domain(content: str) -> str:
        """推断内容领域"""
        lower = content.lower()
        if any(kw in lower for kw in ("代码", "bug", "debug", "技术", "code")):
            return "tech"
        if any(kw in lower for kw in ("项目", "计划", "project", "迭代")):
            return "project"
        if any(kw in lower for kw in ("概念", "原理", "理论", "concept")):
            return "concept"
        return "general"


# ========== WikiIncrementalDistiller ==========

class WikiIncrementalDistiller:
    """Wiki 页面增量更新

    新 Memos 增量合并到已有页面，尊重用户编辑区域。
    """

    USER_EDITED_MARKER = "<!-- user-edited -->"
    AUTO_MAINTAINED_MARKER = "<!-- auto-maintained -->"

    def update_page(self, page_path: Path, new_content: str,
                    mode: str = "append") -> bool:
        """增量更新 Wiki 页面

        Args:
            page_path: 页面路径
            new_content: 新增内容
            mode: append（追加）/ replace（替换自动维护区域）

        Returns:
            是否成功更新
        """
        if not page_path.exists():
            return False

        try:
            existing = page_path.read_text(encoding="utf-8")
        except Exception:
            return False

        if mode == "append":
            updated = self._append_content(existing, new_content)
        elif mode == "replace":
            updated = self._replace_auto_content(existing, new_content)
        else:
            return False

        try:
            page_path.write_text(updated, encoding="utf-8")
            return True
        except Exception:
            return False

    def _append_content(self, existing: str, new_content: str) -> str:
        """追加内容到页面末尾（演化历史之后）"""
        marker = "## 演化历史"
        if marker in existing:
            parts = existing.split(marker, 1)
            today = datetime.now().strftime("%Y-%m-%d")
            new_entry = f"\n- v+: 增量更新（{today}）\n"
            return parts[0] + new_content + "\n\n" + marker + new_entry + parts[1]
        return existing + "\n\n" + new_content

    def _replace_auto_content(self, existing: str, new_content: str) -> str:
        """替换自动维护区域（保留用户编辑区域）"""
        # 查找 <!-- auto-maintained --> ... <!-- /auto-maintained --> 块
        pattern = re.compile(
            r'<!-- auto-maintained -->.*?<!-- /auto-maintained -->',
            re.DOTALL,
        )
        replacement = f"<!-- auto-maintained -->\n{new_content}\n<!-- /auto-maintained -->"
        return pattern.sub(replacement, existing)


# ========== FragmentationDetector ==========

class FragmentationDetector:
    """碎片检测器

    同一实体 3+ 短页面（avg < 800 字）= 碎片化，建议合并。
    """

    MIN_PAGES = 3
    MAX_AVG_CHARS = 800

    def find_fragmented_entities(self, wiki_dir: Path) -> List[Dict]:
        """检测碎片化的实体

        Returns:
            [{entity: str, pages: [Path], total_chars: int, avg_chars: float}]
        """
        pages_by_entity: Dict[str, List[Tuple[Path, int]]] = {}

        for subdir in ["00-Inbox", "03-Tech", "04-Concepts"]:
            md_dir = wiki_dir / subdir
            if not md_dir.exists():
                continue
            for md_file in md_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    # 提取实体关键词
                    keywords = self._extract_page_entity(content, md_file.stem)
                    for kw in keywords:
                        pages_by_entity.setdefault(kw, []).append((md_file, len(content)))
                except Exception:
                    continue

        fragmented = []
        for entity, pages in pages_by_entity.items():
            if len(pages) < self.MIN_PAGES:
                continue
            total_chars = sum(c for _, c in pages)
            avg_chars = total_chars / len(pages)
            if avg_chars < self.MAX_AVG_CHARS:
                fragmented.append({
                    "entity": entity,
                    "pages": [p for p, _ in pages],
                    "total_chars": total_chars,
                    "avg_chars": round(avg_chars, 0),
                })

        return sorted(fragmented, key=lambda x: x["avg_chars"])

    @staticmethod
    def _extract_page_entity(content: str, stem: str) -> List[str]:
        """从页面提取实体关键词"""
        entities = set()
        # 从 frontmatter 提取
        fm_match = re.search(r'^---\n(.*?)\n---', content, re.DOTALL)
        if fm_match:
            fm = fm_match.group(1)
            for line in fm.split("\n"):
                if "核心概念" in line or "工具实体" in line:
                    words = re.findall(r'[\w一-龥]{2,}', line)
                    entities.update(words)
        # 从文件名提取
        name_parts = re.findall(r'[a-zA-Z_]{3,}|[一-龥]{2,4}', stem)
        entities.update(name_parts)
        return list(entities)


# ========== CrossPageDistiller ==========

class CrossPageDistiller:
    """跨页面联合蒸馏 — 检测碎片 + 合并为单页面

    旧页面标记 `superceded`，保留内容折叠在 <details> 中。
    """

    def __init__(self, caller: HostAgentCaller = None):
        self._caller = caller or HostAgentCaller()
        self._fragmentation_detector = FragmentationDetector()

    def detect_and_merge(self, wiki_dir: Path) -> List[Dict]:
        """检测碎片化实体并合并

        Returns:
            [{entity, merged_path, old_pages, status}]
        """
        fragmented = self._fragmentation_detector.find_fragmented_entities(wiki_dir)
        results = []

        for entry in fragmented:
            result = self.distill_merged(
                entry["entity"], entry["pages"], wiki_dir,
            )
            results.append({
                "entity": entry["entity"],
                "merged_path": result,
                "old_pages": [str(p) for p in entry["pages"]],
                "status": "merged" if result else "skipped",
            })

        return results

    def distill_merged(self, entity: str, pages: List[Path],
                       wiki_dir: Path) -> Optional[str]:
        """合并碎片页面

        Args:
            entity: 实体名称
            pages: 待合并的页面路径列表
            wiki_dir: Wiki 根目录

        Returns:
            合并后页面路径，或 None 表示合并失败
        """
        # 收集所有页面内容
        page_contents = []
        for page in pages:
            try:
                content = page.read_text(encoding="utf-8")
                page_contents.append({"path": str(page), "content": content[:3000]})
            except Exception:
                continue

        if not page_contents:
            return None

        # LLM 合并（如果可用）
        merged_content = self._llm_merge(entity, page_contents)

        if not merged_content:
            # 规则级降级：简单拼接
            merged_content = self._simple_merge(entity, page_contents)

        if not merged_content:
            return None

        # 写入合并页面
        merged_path = wiki_dir / "00-Inbox" / f"{entity}_merged.md"
        try:
            merged_path.write_text(merged_content, encoding="utf-8")
        except Exception:
            return None

        # 标记旧页面为 superceded
        for page in pages:
            self._mark_superceded(page, merged_path)

        return str(merged_path)

    def _llm_merge(self, entity: str, pages: List[Dict]) -> Optional[str]:
        """LLM 合并碎片"""
        from core.hephaestus.prompt_builder import (
            PromptBuilder, DistillTask, Session, DeferredRecord,
        )
        backlog = [
            DeferredRecord(session_id=p["path"], agent_name="merge", content=p["content"])
            for p in pages
        ]
        task = DistillTask(
            task_type="merge",
            session_type="general",
            backlog_items=backlog,
        )
        try:
            builder = PromptBuilder()
            prompt = builder.build(task)
            result = self._caller.call(prompt, expect_json=True, max_retries=1, timeout=60)
            if result and "core_content" in result:
                title = result.get("merged_title", entity)
                form = result.get("merged_form", "pattern")
                fm = result.get("frontmatter", {})
                fm["status"] = "merged"
                fm["merged_from"] = [p["path"] for p in pages]

                frontmatter = "---\n"
                for k, v in fm.items():
                    if isinstance(v, list):
                        frontmatter += f"{k}: {json.dumps(v, ensure_ascii=False)}\n"
                    else:
                        frontmatter += f"{k}: {v}\n"
                frontmatter += "---\n\n"

                return frontmatter + f"# {title}\n\n" + result["core_content"]
        except Exception:
            pass
        return None

    def _simple_merge(self, entity: str, pages: List[Dict]) -> str:
        """规则级简单合并（降级方案）"""
        lines = [f"---", f"类型: pattern", f"status: merged",
                 f"merged_from: {json.dumps([p['path'] for p in pages], ensure_ascii=False)}",
                 f"---", "", f"# {entity}", "",
                 "## 合并来源", ""]
        for p in pages:
            lines.append(f"- [[{Path(p['path']).stem}]]")
        lines.append("")
        lines.append("## 核心内容")
        lines.append("")
        for i, p in enumerate(pages, 1):
            content = p["content"]
            # 去除 frontmatter
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    content = content[end + 3:].strip()
            lines.append(f"### 来源 {i}")
            lines.append(content[:1000])
            lines.append("")
        return "\n".join(lines)

    def _mark_superceded(self, page: Path, merged_path: Path):
        """标记旧页面为 superceded"""
        try:
            content = page.read_text(encoding="utf-8")
            # 在 frontmatter 中添加 superceded 标记
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    fm = content[:end + 3]
                    body = content[end + 3:]
                    if "status:" in fm:
                        fm = re.sub(r'status:\s*\w+', 'status: superceded', fm)
                    else:
                        fm = fm.rstrip("-") + "status: superceded\n---"
                    # 折叠旧内容
                    new_content = (
                        fm + f"\n\n> ⚠️ 此页面已被 [[{merged_path.stem}]] 替代。\n\n"
                        f"<details>\n<summary>原始内容（已折叠）</summary>\n\n"
                        f"{body}\n\n</details>"
                    )
                    page.write_text(new_content, encoding="utf-8")
        except Exception:
            pass
