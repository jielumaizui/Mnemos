# -*- coding: utf-8 -*-
"""
LinkProbeWorker — 后台链接可达性探测 Worker

设计原则（重构宪法第三修正案）：
- 不阻塞入库路径：DistillSelfCheck 只做轻量格式校验 + enqueue
- 批量异步探测：后台定时扫描 pending 链接
- 超时 + 重试：单次探测 10s 超时，429/503 指数退避
- 结果持久化：SQLite 队列 + frontmatter 反写

Usage:
    worker = LinkProbeWorker()
    # 入库自检时 enqueue
    worker.enqueue("https://example.com", "wiki/03-Tech/Redis.md")
    # 后台批量探测
    worker.probe_batch(batch_size=50)
    # 或定时运行
    worker.run(interval_seconds=3600)
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.frontmatter import to_chinese_frontmatter, parse_frontmatter, write_frontmatter

from core.config import get_config
from core.cognitive.state_contract import sha256_json
from core.db_utils import delete_older_than
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text

logger = logging.getLogger(__name__)

LINK_PROBE_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:link-probe-frontmatter",
    contract_revision_id="mnemos.link_probe_frontmatter.v1",
    contract_text=(
        "LinkProbeWorker may annotate one exact page preimage with only the exact "
        "broken external links observed in its durable probe queue."
    ),
    source_namespace="link-probe-frontmatter",
    producer="link-probe-worker",
    producer_code_hash=sha256_json(
        {
            "module": "core.hephaestus.link_probe_worker",
            "producer": "update_wiki_frontmatter",
            "version": "mnemos.link_probe_frontmatter.v1",
        }
    ),
    evaluator_id="link-probe-frontmatter-evaluator",
    constraints=(
        "Target, page preimage, URLs, HTTP outcomes, and rendered bytes remain exact.",
        "Only durable broken-link observations may enter broken_links frontmatter.",
    ),
    approved_candidate_key="annotate_exact_broken_links",
    approved_candidate_summary="Annotate the page with the exact observed broken links.",
    rejected_candidate_key="retain_link_frontmatter",
    rejected_candidate_summary="Retain the page when probe evidence or page bytes drift.",
    approved_reason_code="link_probe_binding_verified",
    rejected_reason_code="link_probe_binding_rejected",
    committed_metric="link_probe_frontmatter_committed",
    rejected_metric="unbound_link_probe_frontmatter_count",
)

# 排除的内部/私有地址模式
_INTERNAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}  # nosec B104: explicit localhost/private-host allowlist


def _is_external_url(url: str) -> bool:
    """判断是否为需要探测的外部 URL"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname or ""
        if (
            hostname in _INTERNAL_HOSTS
            or hostname.startswith("192.168.")
            or hostname.startswith("10.")
        ):
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
        logging.getLogger(__name__).warning(
            "Caught unexpected error at link_probe_worker.py", exc_info=True
        )
        return False


class LinkProbeWorker:
    """后台链接可达性探测 Worker"""

    DEFAULT_TIMEOUT = 10
    MAX_RETRIES = 3
    BACKOFF_BASE = 2.0
    USER_AGENT = "Mnemos-LinkProbe/1.0 (+https://github.com/jielumaizui/Mnemos)"

    def __init__(
        self,
        db_path: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        wiki_base: Optional[str | Path] = None,
    ):
        self.config = get_config()
        self.db_path = Path(db_path or (self.config.database_dir / "link_probe.db")).expanduser()
        self.wiki_base = Path(
            wiki_base if wiki_base is not None else self.config.wiki_dir
        ).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.max_retries = max_retries
        self._init_db()
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        """构造带重试策略的 requests Session"""
        session = requests.Session()
        session.headers["User-Agent"] = self.USER_AGENT
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=1,  # urllib3 层重试
                backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["HEAD", "GET"],
            )
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _init_db(self):
        """初始化探测队列数据库"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS link_probe_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    page_path TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    http_status INTEGER,
                    probe_error TEXT,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_probed TIMESTAMP,
                    retry_count INTEGER DEFAULT 0,
                    UNIQUE(url, page_path)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_link_probe_status
                ON link_probe_queue(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_link_probe_page
                ON link_probe_queue(page_path)
            """)
            conn.commit()

    # ---------- 队列操作 ----------

    def enqueue(self, url: str, page_path: str) -> bool:
        """
        将链接加入探测队列（入库自检时调用）。
        只 enqueue 外部 URL，内部地址跳过。
        不阻塞，O(1)。
        """
        if not _is_external_url(url):
            return False
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO link_probe_queue (url, page_path, status)
                    VALUES (?, ?, 'pending')
                """,
                    (url, page_path),
                )
                conn.commit()
            return True
        except (sqlite3.Error, OSError) as e:
            logger.debug("[LinkProbe] enqueue 失败 %s: %s", url, e, exc_info=True)
            return False

    def enqueue_from_content(self, content: str, page_path: str) -> int:
        """
        从内容中提取所有 URL 并批量 enqueue。
        返回成功入队数量。
        """
        import re

        urls = re.findall(r'https?://[^\s)\]\>"]+', content)
        count = 0
        for url in urls:
            if self.enqueue(url, page_path):
                count += 1
        return count

    # ---------- 探测核心 ----------

    def probe_single(self, url: str) -> Tuple[str, Optional[int], Optional[str]]:
        """
        探测单个链接的可达性。

        Returns:
            (status, http_status, error_msg)
            status: reachable / broken / timeout / error
        """
        try:
            resp = self._session.head(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                headers={"Accept": "*/*"},
            )
            # 405 Method Not Allowed 时回退到 GET
            if resp.status_code == 405:
                resp = self._session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    stream=True,  # 不下载内容体
                    headers={"Accept": "*/*"},
                )
                resp.close()

            if resp.status_code < 400:
                return "reachable", resp.status_code, None
            elif resp.status_code in (429, 503):
                return "retryable", resp.status_code, f"HTTP {resp.status_code}"
            else:
                return "broken", resp.status_code, f"HTTP {resp.status_code}"
        except requests.Timeout:
            return "timeout", None, f"超时 ({self.timeout}s)"
        except requests.ConnectionError as e:
            return "broken", None, f"连接失败: {e}"
        except requests.RequestException as e:
            return "error", None, str(e)

    def probe_batch(
        self,
        batch_size: int = 50,
        max_retryable: int = 10,
    ) -> Dict[str, Any]:
        """
        批量探测 pending 链接。

        Args:
            batch_size: 每批最大探测数
            max_retryable: 本轮最多处理多少个 retryable（避免无限循环）

        Returns:
            {"probed": int, "reachable": int, "broken": int, "retryable": int, "errors": int}
        """
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.cursor()
            # 优先探测 pending，其次探测 retryable 但 retry_count < max_retries
            cursor.execute(
                """
                SELECT id, url, retry_count FROM link_probe_queue
                WHERE status IN ('pending', 'retryable')
                  AND retry_count < ?
                ORDER BY status = 'pending' DESC, first_seen ASC
                LIMIT ?
            """,
                (self.max_retries, batch_size),
            )
            rows = cursor.fetchall()

        stats = {"probed": 0, "reachable": 0, "broken": 0, "retryable": 0, "errors": 0}
        retryable_handled = 0

        for row_id, url, retry_count in rows:
            # 限制 retryable 处理数量
            if retry_count > 0:
                if retryable_handled >= max_retryable:
                    continue
                retryable_handled += 1
                # 指数退避：等待 2^retry_count 秒
                time.sleep(self.BACKOFF_BASE**retry_count)

            status, http_status, error_msg = self.probe_single(url)
            stats["probed"] += 1

            now = datetime.now().isoformat()
            new_retry_count = retry_count + 1 if status == "retryable" else retry_count

            if status == "reachable":
                stats["reachable"] += 1
            elif status == "retryable":
                stats["retryable"] += 1
            elif status == "broken":
                stats["broken"] += 1
            else:
                stats["errors"] += 1

            # 更新数据库
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """
                    UPDATE link_probe_queue
                    SET status = ?, http_status = ?, probe_error = ?,
                        last_probed = ?, retry_count = ?
                    WHERE id = ?
                """,
                    (status, http_status, error_msg, now, new_retry_count, row_id),
                )
                conn.commit()

            # 极短间隔，避免被目标站限流
            time.sleep(0.2)

        logger.info("[LinkProbe] 批次完成: %s", stats)
        return stats

    # ---------- 查询接口 ----------

    def get_broken_links_for_page(self, page_path: str) -> List[Dict[str, Any]]:
        """获取某页面的失效链接列表"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row  # noqa
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT url, http_status, probe_error, last_probed
                FROM link_probe_queue
                WHERE page_path = ? AND status IN ('broken', 'timeout', 'error')
                ORDER BY last_probed DESC
            """,
                (page_path,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """清理/统计 first_seen 早于保留期限的链接探测队列记录。"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            return delete_older_than(conn, "link_probe_queue", "first_seen", days, dry_run=dry_run)

    def get_pending_count(self) -> int:
        """待探测链接数量"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM link_probe_queue WHERE status IN ('pending', 'retryable')"
            )
            return cursor.fetchone()[0]  # type: ignore[no-any-return]

    def get_stats(self) -> Dict[str, int]:
        """队列整体统计"""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, COUNT(*) FROM link_probe_queue GROUP BY status
            """)
            return {r[0]: r[1] for r in cursor.fetchall()}

    # ---------- 后台运行 ----------

    def run_once(self) -> Dict[str, Any]:
        """单次运行（适合 cron / launchd 调用）"""
        pending = self.get_pending_count()
        if pending == 0:
            return {"probed": 0, "note": "no_pending"}
        return self.probe_batch(batch_size=50)

    # ---------- Wiki frontmatter 反写 ----------

    def update_wiki_frontmatter(self, page_path: str) -> bool:
        """
        将探测结果反写到 wiki 页面的 frontmatter 中。
        由调用方在 probe_batch 后批量执行。
        """
        broken = self.get_broken_links_for_page(page_path)
        if not broken:
            return False

        # 尝试更新 frontmatter（如果页面存在）
        wiki_path = Path(page_path)
        if not wiki_path.exists():
            return False

        try:
            content = wiki_path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            if fm is None:
                fm = {}
            fm["broken_links"] = [b["url"] for b in broken]
            fm = to_chinese_frontmatter(fm)
            new_content = write_frontmatter(fm, body)
            evidence_refs = [f"link_probe:{b['url']}" for b in broken[:5]]
            submit_or_write_markdown_with_decision(
                decision_policy=LINK_PROBE_MARKDOWN_POLICY,
                decision_facts={
                    "schema_version": "mnemos.link_probe_frontmatter_facts.v1",
                    "broken_links": [
                        {
                            "url": item["url"],
                            "http_status": item.get("http_status"),
                            "probe_error": item.get("probe_error", ""),
                            "last_probed": item.get("last_probed", ""),
                        }
                        for item in broken
                    ],
                },
                decision_task=f"Annotate broken links for {wiki_path.name}",
                decision_goal="Keep page link-health metadata aligned with durable probes.",
                decision_created_at=datetime.now(timezone.utc).isoformat(),
                wiki_base=self.wiki_base,
                target_path=wiki_path,
                content=new_content,
                source="link_probe_worker",
                actor="system",
                evidence_refs=evidence_refs,
                proposed_action="update_broken_link_frontmatter",
                expected_existing_hash=sha256_text(content),
                metadata={"broken_link_count": len(broken)},
            )
            return True
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.debug("[LinkProbe] frontmatter 更新失败 %s: %s", page_path, e, exc_info=True)
        return False


def get_link_probe_worker(config=None) -> Optional["LinkProbeWorker"]:
    """根据配置返回 LinkProbeWorker 实例；未启用或初始化失败时返回 None。"""
    cfg = config or get_config()
    if not cfg.get("features.enable_link_probe", False):
        return None
    try:
        return LinkProbeWorker()
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
        logger.warning("[LinkProbe] Worker 初始化失败，链接探测功能已禁用", exc_info=True)
        return None
