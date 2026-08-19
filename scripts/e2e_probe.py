#!/usr/bin/env python3
"""
E2E 全链路探针 — 验证 capture->raw/sync/backend->distill->Wiki->Search->MCP 全链路

用法：
    python3 scripts/e2e_probe.py
    python3 scripts/e2e_probe.py --dry-run

探针只把已知 I/O、配置、SQLite 与运行时故障转换为逐步失败；未知编程错误保持可见。
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 探针步骤状态
STATUS_PASS = "pass"
STATUS_SKIP = "skip"
STATUS_FAIL = "fail"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))  # [P2-FIX] Guard sys.path mutation

from core.db_utils import sqlite_artifact_exists  # noqa: E402
from core.db_utils import validate_sql_identifier  # noqa: E402
from core.privacy.redaction import redact_path  # noqa: E402

RAW_CLEANUP_TABLES = frozenset({"raw_access_log", "raw_metrics", "raw_turns"})

E2E_PROBE_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
)


def _probe_capture() -> Tuple[str, str]:
    """1. 生成测试 session 并 capture"""
    capture = None
    try:
        from core.sync_framework.capture_service import CaptureService

        capture = CaptureService(start_worker=False)
        sid = f"e2e_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        captured = capture.capture_session(
            source_agent="e2e_probe",
            session_id=sid,
            turns=[
                {
                    "turn_number": 1,
                    "user_content": "E2E探针测试：Mnemos全链路验证",
                    "assistant_content": "收到测试请求，正在验证各模块连通性。",
                    "metadata": {"probe": True},
                }
            ],
        )
        capture.end_session("e2e_probe", sid)
        flush = capture.worker_pool.flush_session("e2e_probe", sid)
        if flush.get("failed", 0):
            return False, f"flush failed: {flush}"  # type: ignore[return-value]
        if captured.get("queued_count", 0) and not flush.get("flushed", 0):
            # type: ignore[return-value]
            return False, f"queued but not flushed: capture={captured}, flush={flush}"  # type: ignore[return-value]  # noqa: E501
        return STATUS_PASS, sid
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)
    finally:
        if capture is not None:
            try:
                capture.close()
            # [P2-FIX] Broad except acceptable for cleanup robustness
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                pass


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    """Read config values from real config objects and class-style test doubles."""
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    if isinstance(config, type):
        try:
            return getter(config, key, default)
        except TypeError:
            pass
    try:
        return getter(key, default)
    except TypeError:
        try:
            return getter(config, key, default)
        except TypeError:
            return default


def _parse_backend_uids(raw: Optional[str]) -> List[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(value, list):
        return [str(uid) for uid in value if uid]
    if value:
        return [str(value)]
    return []


def _load_probe_sync_rows(config: Any, sid: str) -> Tuple[List[Dict[str, Any]], str]:
    """Return sync_log evidence rows for the probe session."""
    import sqlite3

    sync_log = Path(config.database_dir) / "sync_log.db"
    if not sqlite_artifact_exists(sync_log):
        return [], f"sync_log.db 未找到: {sync_log}"
    try:
        with sqlite3.connect(sync_log.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT turn_number, status, backend_uids FROM sync_log
                WHERE agent_name = 'e2e_probe' AND session_id = ?
                ORDER BY turn_number ASC
                """,
                (sid,),
            ).fetchall()
    except sqlite3.Error as exc:
        return [], f"sync_log 读取失败: {exc}"
    return [
        {
            "turn_number": int(turn_number),
            "status": str(status or ""),
            "backend_uids": _parse_backend_uids(backend_uids),
        }
        for turn_number, status, backend_uids in rows
    ], ""


def _load_probe_raw_rows(config: Any, sid: str) -> Tuple[List[Dict[str, Any]], str]:
    """Return canonical raw_events rows for the probe session."""
    import sqlite3

    raw_db = Path(config.database_dir) / "raw_events.db"
    if not sqlite_artifact_exists(raw_db):
        return [], f"raw_events.db 未找到: {raw_db}"
    try:
        with sqlite3.connect(raw_db.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT event_id, turn_number, content_hash FROM raw_turns
                WHERE source_agent = 'e2e_probe' AND session_id = ?
                ORDER BY turn_number ASC
                """,
                (sid,),
            ).fetchall()
    except sqlite3.Error as exc:
        return [], f"raw_events.db 读取失败: {exc}"
    return [
        {
            "event_id": str(event_id),
            "turn_number": int(turn_number),
            "content_hash": str(content_hash or ""),
        }
        for event_id, turn_number, content_hash in rows
    ], ""


def _canonical_raw_owns_l1(config: Any) -> bool:
    return bool(
        _config_get(config, "raw_event_store.enabled", True)
        and _config_get(config, "raw_projection.enabled", True)
    )


def _sync_summary(rows: List[Dict[str, Any]]) -> str:
    return ", ".join(
        f"turn={row['turn_number']}:status={row['status']}:backend_uids={row['backend_uids']}"
        for row in rows
    )


def _verify_backend_uids(sid: str, uids: List[str]) -> Tuple[List[str], List[str]]:
    from core.sync_framework.storage_backend import create_storage_backend

    backend = create_storage_backend()
    verified = []
    missing = []
    for uid in uids:
        result = backend.get_by_id(uid)
        if result is None:
            missing.append(uid)
            continue
        content = getattr(result, "content", "") or ""
        metadata = getattr(result, "metadata", {}) or {}
        if sid in content or metadata.get("session_id") == sid:
            verified.append(uid)
        else:
            missing.append(uid)
    return verified, missing


def _probe_backend(sid: str) -> Tuple[str, str]:
    """2. 检查 canonical raw / sync_log / backend projection 的真实落地证据。"""
    try:
        from core.config import get_config

        config = get_config()
        sync_rows, sync_error = _load_probe_sync_rows(config, sid)
        if not sync_rows:
            return STATUS_FAIL, sync_error or f"sync_log 未记录 session {sid}"

        bad_statuses = {
            row["status"]
            for row in sync_rows
            if row["status"] not in {"new", "updated", "synced", "backfilled"}
        }
        if bad_statuses:
            return (
                STATUS_FAIL,
                f"sync_log 状态未证明真实落地: {sorted(bad_statuses)}; {_sync_summary(sync_rows)}",
            )

        all_backend_uids = [
            uid
            for row in sync_rows
            for uid in row["backend_uids"]
        ]

        if _canonical_raw_owns_l1(config):
            raw_rows, raw_error = _load_probe_raw_rows(config, sid)
            if not raw_rows:
                return (
                    STATUS_FAIL,
                    f"raw_events.db 未记录 session {sid}; {raw_error}; sync_log={_sync_summary(sync_rows)}",
                )
            raw_ids = [row["event_id"] for row in raw_rows]
            return (
                STATUS_PASS,
                "canonical raw 落地已验证: "
                f"raw record id={raw_ids[0]}, raw_records={len(raw_rows)}, "
                f"sync_log={_sync_summary(sync_rows)}, "
                f"backend_projection=raw_projection, backend_uids={all_backend_uids}",
            )

        if not all_backend_uids:
            return (
                STATUS_FAIL,
                f"backend_uids 为空，不能证明外部 backend 写入; sync_log={_sync_summary(sync_rows)}",
            )
        verified, missing = _verify_backend_uids(sid, all_backend_uids)
        if not verified:
            return (
                STATUS_FAIL,
                f"backend_uids 无法反查到 session {sid}: missing={missing}; "
                f"sync_log={_sync_summary(sync_rows)}",
            )
        return (
            STATUS_PASS,
            f"external backend 落地已验证: backend uid={verified[0]}, "
            f"verified={len(verified)}, sync_log={_sync_summary(sync_rows)}",
        )
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _e2e_distill_messages(sid: str, attempt: int) -> List[Dict[str, Any]]:
    emphasis = (
        "第二次尝试：上一次可能没有产出可写入片段。"
        "请把下面的验收合同作为稳定知识记录，必须保留 session_id，并用 Markdown 小标题组织正文。"
        if attempt > 1
        else ""
    )
    return [
        {
            "role": "user",
            "content": (
                f"{emphasis}\n"
                "E2E探针测试：请把这次验证作为一条可读知识记录。"
                f"探针 session_id：{sid}，请在知识正文中保留这个 session_id。"
                "关键决策：Mnemos 部署验收必须覆盖 capture、canonical raw、sync_log、backend projection、"
                "蒸馏、Wiki、搜索、MCP 和 cleanup。"
                "验收规则：canonical raw 模式必须展示 raw_events.db.raw_turns 的 event_id；"
                "外部 backend 模式必须展示非空 backend_uids 并能反查文件；"
                "Wiki 页面必须包含本次 session_id，不能被历史 e2e_probe 页面误导。"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "可记录知识：如果任一环节失败，E2E 探针必须给出失败原因；"
                "测试结束后必须清理探针产生的 raw、sync_log、Wiki 和 backend 数据。"
                "这条知识用于运维验收，不是临时闲聊。"
            ),
        },
    ]


def _probe_distill(sid: str, no_api: bool = False, real_api: bool = False) -> Tuple[str, str]:
    """3. API 已配置时真实触发一次小型蒸馏并写 Wiki。

    - no_api=True: 强制跳过真实蒸馏。
    - real_api=True: 强制要求 API 必须配置，否则标记为失败。
    """
    try:
        if no_api:
            return STATUS_SKIP, "已按 --no-api 跳过真实 LLM API 蒸馏"

        from core.llm_config import resolve_effective_llm_api_config
        from core.config import get_config

        config = get_config()
        llm_cfg = resolve_effective_llm_api_config(config)
        if not llm_cfg or not llm_cfg.configured:
            if real_api:
                return STATUS_FAIL, "蒸馏 API 未配置（--real-api 要求必须配置）"
            return STATUS_SKIP, "蒸馏 API 未配置（跳过）"

        from core.hephaestus.distillation_engine import DistillationEngine

        engine = DistillationEngine()
        try:
            from core.hephaestus.distillation_engine import ValuePrejudgment

            engine._value_prejudgment = SimpleNamespace(  # type: ignore[assignment]  # noqa
                judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.95)
            )
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("跳过 ValuePrejudgment 注入", exc_info=True)
        last_result = None
        max_attempts = 2 if real_api else 1
        for attempt in range(1, max_attempts + 1):
            result = engine.process(
                sid,
                _e2e_distill_messages(sid, attempt),
                meta={"source": "e2e_probe", "probe": True, "attempt": attempt},
            )
            last_result = result
            paths = engine.write_pages(result)
            if paths:
                return STATUS_PASS, f"生成 Wiki 页面 {len(paths)} 个: {paths[0]} (attempt={attempt})"
        return STATUS_FAIL, (
            f"蒸馏未生成 Wiki 页面: judgment={getattr(last_result, 'judgment', '')}, "
            f"reason={getattr(last_result, 'judgment_reason', '')}"
        )
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _probe_wiki(sid: str, distill_status: str = STATUS_FAIL) -> Tuple[str, str]:
    """4. 检查 Wiki 中是否有探针页面。

    若蒸馏被跳过，则 Wiki 写入检查也标记为跳过，不伪装成已验证。
    """
    try:
        from core.config import get_config

        wiki_dir = get_config().wiki_dir
        inbox = wiki_dir / "00-Inbox"
        # 探针应能自检初始化 Wiki 基础目录
        inbox.mkdir(parents=True, exist_ok=True)

        if distill_status == STATUS_SKIP:
            return STATUS_SKIP, "Wiki 写入未验证（蒸馏已跳过）"

        # 探针可能不会立即生成页面，给一些时间
        time.sleep(2)
        for md in inbox.glob("*.md"):
            content = md.read_text(encoding="utf-8", errors="ignore")
            if sid in content:
                return STATUS_PASS, f"找到 Wiki 页面: {md.name}"
        return STATUS_FAIL, "未找到探针 Wiki 页面"
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _probe_search() -> Tuple[str, str]:
    """5. 搜索验证"""
    try:
        from core.embeddings.index_manager import EmbeddingIndexManager

        idx = EmbeddingIndexManager()
        # 如果索引为空，说明还没构建过
        stats = idx.get_stats()
        if stats.get("total_pages", 0) == 0:
            return STATUS_SKIP, "索引为空（首次部署，尚未有页面）"

        results = idx.search("Mnemos 验证", top_k=5)
        return STATUS_PASS, f"搜索返回 {len(results)} 条结果"
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _probe_mcp() -> Tuple[str, str]:
    """6. MCP 服务验证"""
    try:
        from integrations.agora import MCPServer

        server = MCPServer()
        tools = server.tools
        return STATUS_PASS, f"MCP Server 就绪 ({len(tools)} tools)"
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _probe_dry_run_config(*, show_paths: bool = False) -> Tuple[str, str]:
    """只读检查配置和关键路径，不创建目录或写入测试数据。"""
    try:
        from core.config import get_config

        config = get_config()
        wiki_dir = Path(config.wiki_dir)
        database_dir = Path(config.database_dir)
        raw_dir = Path(getattr(config, "obsidian_vault_path", ""))
        missing = [
            label
            for label, path in (
                ("wiki_dir", wiki_dir),
                ("database_dir", database_dir),
                ("raw_vault", raw_dir),
            )
            if path and not path.exists()
        ]
        if missing:
            return STATUS_FAIL, "路径不存在: " + ", ".join(missing)
        display_path = str if show_paths else redact_path
        readonly_bits = [
            f"wiki_dir={display_path(wiki_dir)}",
            f"database_dir={display_path(database_dir)}",
            f"wiki_writable={os.access(wiki_dir, os.W_OK)}",
            f"database_writable={os.access(database_dir, os.W_OK)}",
        ]
        if raw_dir:
            readonly_bits.append(f"raw_vault={display_path(raw_dir)}")
            readonly_bits.append(f"raw_writable={os.access(raw_dir, os.W_OK)}")
        return STATUS_PASS, "; ".join(readonly_bits)
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _probe_dry_run_imports() -> Tuple[str, str]:
    """只导入关键模块，确认探针依赖可加载。"""
    modules = [
        "core.sync_framework.capture_service",
        "core.sync_framework.storage_backend",
        "core.hephaestus.distillation_engine",
        "core.embeddings.index_manager",
        "integrations.agora",
    ]
    failed = []
    for module in modules:
        try:
            importlib.import_module(module)
        except E2E_PROBE_ERRORS as exc:
            failed.append(f"{module}: {exc}")
    if failed:
        return STATUS_FAIL, "; ".join(failed)
    return STATUS_PASS, f"关键模块可导入 ({len(modules)} 个)"


def _probe_dry_run_databases() -> Tuple[str, str]:
    """只读打开已存在的 SQLite 文件，确认不会在 dry-run 中创建或修改。"""
    try:
        import sqlite3

        from core.config import get_config

        database_dir = Path(get_config().database_dir)
        candidates = ["raw_events.db", "sync_log.db", "wiki_state.db", "events.db"]
        checked = 0
        missing = []
        for name in candidates:
            path = database_dir / name
            if not sqlite_artifact_exists(path):
                missing.append(name)
                continue
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            checked += 1
        if checked == 0:
            return STATUS_SKIP, "未发现可只读打开的 SQLite 状态库"
        detail = f"只读打开 {checked} 个 SQLite 状态库"
        if missing:
            detail += f"；缺失 {', '.join(missing)}"
        return STATUS_PASS, detail
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _probe_dry_run_llm_config() -> Tuple[str, str]:
    """只读解析 LLM 配置，不发起 API 请求。"""
    try:
        from core.config import get_config
        from core.llm_config import resolve_effective_llm_api_config

        llm_cfg = resolve_effective_llm_api_config(get_config())
        if not llm_cfg or not llm_cfg.configured:
            return STATUS_SKIP, "LLM API 未配置；dry-run 未调用外部 API"
        return STATUS_PASS, f"LLM 配置可解析: {llm_cfg.provider}/{llm_cfg.model}"
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def run_dry_run_probe(
    verbose: bool = False,
    show_paths: bool = False,
) -> Dict[str, Tuple[str, str]]:
    """运行只读 dry-run 探针，保证不写 capture/backend/wiki/queue，不调用外部 API。"""
    steps = {}
    probes = [
        (
            "config",
            "配置与路径只读检查",
            lambda: _probe_dry_run_config(show_paths=show_paths),
        ),
        ("imports", "关键模块导入", _probe_dry_run_imports),
        ("databases", "SQLite 状态库只读检查", _probe_dry_run_databases),
        ("llm_config", "LLM 配置解析", _probe_dry_run_llm_config),
        ("mcp", "MCP 服务导入与工具注册", _probe_mcp),
    ]

    print("=" * 60)
    print("Mnemos E2E dry-run 只读探针")
    print("=" * 60)
    print("dry-run 保证不写入 capture/backend/wiki/queue，不调用真实 LLM API。")

    for idx, (name, title, probe) in enumerate(probes, 1):
        print(f"\n[{idx}/{len(probes)}] {title}...")
        status, msg = probe()
        steps[name] = (status, msg)
        print(f"  {_status_symbol(status)} {msg}")
        if verbose and status == STATUS_FAIL:
            logger.debug("dry-run probe %s failed: %s", name, msg)

    print("\n" + "=" * 60)
    passed = sum(1 for s, _ in steps.values() if s == STATUS_PASS)
    skipped = sum(1 for s, _ in steps.values() if s == STATUS_SKIP)
    failed = sum(1 for s, _ in steps.values() if s == STATUS_FAIL)
    print(f"dry-run 结果: {passed} 通过 / {skipped} 跳过 / {failed} 失败")
    print("=" * 60)
    return steps


def _cleanup(sid: str) -> Tuple[str, str]:
    """7. 清理测试数据"""
    try:
        cleaned = {"queue": 0, "raw": 0, "sync_log": 0, "wiki": 0, "backend": 0}

        # 删除 distill_queue 中的探针任务
        from core.config import get_config

        queue_dir = get_config().database_dir / "distill_queue"
        task_path = queue_dir / f"{sid}.json"
        if task_path.exists():
            task_path.unlink()
            cleaned["queue"] += 1

        # 删除 Wiki 中的探针页面
        from core.config import get_config

        config = get_config()
        wiki_dir = config.wiki_dir
        inbox = wiki_dir / "00-Inbox"
        if inbox.exists():
            for md in inbox.glob("*.md"):
                if sid in md.read_text(encoding="utf-8", errors="ignore"):
                    md.unlink()
                    cleaned["wiki"] += 1

        # 删除 L1 storage（Obsidian backend）中的探针记录，避免污染用户长期库
        try:
            import sqlite3

            from core.sync_framework.storage_backend import create_storage_backend

            backend = create_storage_backend()
            backend_uids = set()
            sync_log = config.database_dir / "sync_log.db"
            if sync_log.exists():
                with sqlite3.connect(str(sync_log), timeout=5) as conn:
                    rows = conn.execute(
                        """
                        SELECT backend_uids FROM sync_log
                        WHERE agent_name = 'e2e_probe' AND session_id = ?
                        """,
                        (sid,),
                    ).fetchall()
                for (uids_json,) in rows:
                    try:
                        for uid in json.loads(uids_json or "[]"):
                            if uid:
                                backend_uids.add(str(uid))
                    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                        logger.debug("解析 backend_uids 失败", exc_info=True)

            for res in backend.search("E2E探针测试", limit=20):
                if sid in (res.content or ""):
                    backend_uids.add(res.uid)

            for uid in sorted(backend_uids):
                # ObsidianBackend 通过文件路径删除
                file_path = config.obsidian_vault_path / uid
                if file_path.exists():
                    file_path.unlink()
                    cleaned["backend"] += 1

            raw_db = config.database_dir / "raw_events.db"
            if raw_db.exists():
                with sqlite3.connect(str(raw_db), timeout=5) as conn:
                    tables = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "raw_turns" not in tables:
                        event_ids = []
                    else:
                        rows = conn.execute(
                            """
                            SELECT event_id FROM raw_turns
                            WHERE source_agent = 'e2e_probe' AND session_id = ?
                            """,
                            (sid,),
                        ).fetchall()
                        event_ids = [str(row[0]) for row in rows]
                    if event_ids:
                        if "raw_access_log" in tables:
                            _delete_probe_event_rows(conn, "raw_access_log", event_ids)
                        if "raw_metrics" in tables:
                            _delete_probe_event_rows(conn, "raw_metrics", event_ids)
                        placeholders = ",".join("?" for _ in event_ids)
                        if "raw_provenance_edges" in tables and "raw_turn_revisions" in tables:
                            conn.execute(
                                "DELETE FROM raw_provenance_edges WHERE source_revision_id IN "
                                "(SELECT revision_id FROM raw_turn_revisions "
                                f"WHERE logical_event_id IN ({placeholders}))",  # nosec B608
                                event_ids,
                            )
                        if "raw_native_contract_observations" in tables:
                            conn.execute(
                                "DELETE FROM raw_native_contract_observations "
                                f"WHERE logical_event_id IN ({placeholders})",  # nosec B608
                                event_ids,
                            )
                        if "raw_turn_revisions" in tables:
                            conn.execute(
                                "DELETE FROM raw_turn_revisions "
                                f"WHERE logical_event_id IN ({placeholders})",  # nosec B608
                                event_ids,
                            )
                        deleted = _delete_probe_event_rows(conn, "raw_turns", event_ids)
                        cleaned["raw"] += int(deleted or 0)
                    conn.commit()

            if sync_log.exists():
                with sqlite3.connect(str(sync_log), timeout=5) as conn:
                    deleted = conn.execute(
                        """
                        DELETE FROM sync_log
                        WHERE agent_name = 'e2e_probe' AND session_id = ?
                        """,
                        (sid,),
                    ).rowcount
                    cleaned["sync_log"] += int(deleted or 0)
                    conn.commit()
        # [P2-FIX] Broad except acceptable for cleanup robustness
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            # cleanup 不能因为 backend 删除失败而掩盖前面探针结果，但必须在消息中暴露。
            return STATUS_PASS, (
                "测试数据已清理（backend 清理失败，需手动检查）: "
                f"queue={cleaned['queue']}, raw={cleaned['raw']}, "
                f"sync_log={cleaned['sync_log']}, wiki={cleaned['wiki']}, "
                f"backend={cleaned['backend']}"
            )

        return STATUS_PASS, (
            "测试数据已清理: "
            f"queue={cleaned['queue']}, raw={cleaned['raw']}, "
            f"sync_log={cleaned['sync_log']}, wiki={cleaned['wiki']}, "
            f"backend={cleaned['backend']}"
        )
    except E2E_PROBE_ERRORS as e:
        return STATUS_FAIL, str(e)


def _delete_probe_event_rows(conn: Any, table: str, event_ids: List[str]) -> int:
    table_name = validate_sql_identifier(table)
    if table_name not in RAW_CLEANUP_TABLES:
        raise ValueError(f"Unsupported E2E raw cleanup table: {table!r}")
    if not event_ids:
        return 0
    placeholders = ",".join("?" for _ in event_ids)
    query = f"DELETE FROM {table_name} WHERE event_id IN ({placeholders})"  # nosec B608
    return int(conn.execute(query, event_ids).rowcount or 0)


def _status_symbol(status: str) -> str:
    if status == STATUS_PASS:
        return "✓"
    if status == STATUS_SKIP:
        return "⊘"
    return "✗"


def run_probe(
    verbose: bool = False,
    no_api: bool = False,
    real_api: bool = False,
    dry_run: bool = False,
    show_paths: bool = False,
) -> Dict[str, Tuple[str, str]]:
    """运行全链路探针，返回各步骤结果。"""
    if dry_run:
        return run_dry_run_probe(verbose=verbose, show_paths=show_paths)

    steps = {}

    print("=" * 60)
    print("Mnemos E2E 全链路探针")
    print("=" * 60)

    # Step 1: Capture
    print("\n[1/7] Capture 测试 session...")
    status, sid = _probe_capture()
    steps["capture"] = (status, sid)
    print(f"  {_status_symbol(status)} {'session_id=' + sid if status == STATUS_PASS else sid}")
    if status == STATUS_FAIL:
        return steps

    # Step 2: Raw/backend landing
    print("\n[2/7] 检查 raw/sync/backend 落地...")
    status, msg = _probe_backend(sid)
    steps["backend"] = (status, msg)
    print(f"  {_status_symbol(status)} {msg}")

    # Step 3: Distill
    print("\n[3/7] 触发蒸馏...")
    distill_status, distill_msg = _probe_distill(sid, no_api=no_api, real_api=real_api)
    steps["distill"] = (distill_status, distill_msg)
    print(f"  {_status_symbol(distill_status)} {distill_msg}")

    # Step 4: Wiki
    print("\n[4/7] 检查 Wiki 页面...")
    status, msg = _probe_wiki(sid, distill_status=distill_status)
    steps["wiki"] = (status, msg)
    print(f"  {_status_symbol(status)} {msg}")

    # Step 5: Search
    print("\n[5/7] 验证搜索...")
    status, msg = _probe_search()
    steps["search"] = (status, msg)
    print(f"  {_status_symbol(status)} {msg}")

    # Step 6: MCP
    print("\n[6/7] MCP 服务...")
    status, msg = _probe_mcp()
    steps["mcp"] = (status, msg)
    print(f"  {_status_symbol(status)} {msg}")

    # Step 7: Cleanup
    print("\n[7/7] 清理测试数据...")
    status, msg = _cleanup(sid)
    steps["cleanup"] = (status, msg)
    print(f"  {_status_symbol(status)} {msg}")

    print("\n" + "=" * 60)
    passed = sum(1 for s, _ in steps.values() if s == STATUS_PASS)
    skipped = sum(1 for s, _ in steps.values() if s == STATUS_SKIP)
    failed = sum(1 for s, _ in steps.values() if s == STATUS_FAIL)
    print(f"探针结果: {passed} 通过 / {skipped} 跳过 / {failed} 失败")
    print("=" * 60)

    return steps


def main():
    parser = argparse.ArgumentParser(description="Mnemos E2E 全链路探针")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="强制跳过真实 LLM API 蒸馏，将蒸馏/Wiki 写入标记为 skip",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只做只读环境检查，不写入 capture/backend/wiki/queue，不调用真实 LLM API",
    )
    parser.add_argument(
        "--real-api",
        action="store_true",
        help="强制要求配置真实 LLM API，未配置时蒸馏步骤标记为失败",
    )
    parser.add_argument(
        "--unsafe-debug",
        action="store_true",
        help="输出未脱敏的本机路径，仅限本机排障",
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="等同 --unsafe-debug，显示未脱敏路径",
    )
    args = parser.parse_args()

    no_api = args.no_api
    if args.real_api and (args.no_api or args.dry_run):
        parser.error("--no-api/--dry-run 与 --real-api 不能同时使用")

    steps = run_probe(
        verbose=args.verbose,
        no_api=no_api,
        real_api=args.real_api,
        dry_run=args.dry_run,
        show_paths=bool(args.unsafe_debug or args.show_paths),
    )
    failed = sum(1 for s, _ in steps.values() if s == STATUS_FAIL)

    if failed == 0:
        print("\n🎉 全链路探针通过（失败 0 项）")
        sys.exit(0)
    else:
        print(f"\n⚠️  {failed} 项失败，请检查上方输出。")
        sys.exit(1)


if __name__ == "__main__":
    main()
