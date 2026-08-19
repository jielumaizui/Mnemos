# -*- coding: utf-8 -*-
"""盲区管理 CLI 命令"""

from datetime import datetime, timedelta
from pathlib import Path

from core.app.blindspot_asset_schema import BlindspotAssetSchemaError
from core.app.blindspot_discovery import BlindspotDiscovery
from core.config import get_config


def _get_db_path() -> Path:
    return get_config().database_dir / "blindspots.db"


def _list_blindspots(status_filter: str = ""):
    db_path = _get_db_path()
    if not db_path.exists():
        print("暂无盲区数据库")
        return []
    try:
        return BlindspotDiscovery(db_path=str(db_path), initialize=False).list_current(
            status_filter=status_filter
        )
    except BlindspotAssetSchemaError as exc:
        print(f"盲区数据库需要显式对账: {exc}")
        return []


def _print_blindspots(rows: list):
    if not rows:
        print("暂无盲区记录")
        return

    print(f"{'topic':<30} {'status':<14} {'conf':<6} {'detected_at':<26} {'revision_id'}")
    print("-" * 100)
    for row in rows:
        topic = (row.get("topic") or "")[:28]
        status = (row.get("status") or "")[:12]
        conf = row.get("confidence") or 0.0
        detected = (row.get("detected_at") or "")[:24]
        revision_id = (row.get("revision_id") or "")[:30]
        print(f"{topic:<30} {status:<14} {conf:<6.2f} {detected:<26} {revision_id}")


def cmd_blindspot(args) -> int:
    """处理 `mnemos blindspot` 子命令"""
    cmd = getattr(args, "blindspot_cmd", None)

    if cmd == "list":
        status_filter = getattr(args, "status", "")
        rows = _list_blindspots(status_filter=status_filter)
        _print_blindspots(rows)
        return 0

    if cmd == "status":
        db_path = _get_db_path()
        if not db_path.exists():
            print("暂无盲区数据库")
            return 0

        try:
            by_status = BlindspotDiscovery(
                db_path=str(db_path), initialize=False
            ).status_counts()
        except BlindspotAssetSchemaError as exc:
            print(f"盲区数据库需要显式对账: {exc}")
            return 1

        print(f"盲区总数: {sum(by_status.values())}")
        print("按状态分布:")
        for status, count in sorted(by_status.items()):
            print(f"  {status}: {count}")
        return 0

    if cmd == "ignore":
        topic = getattr(args, "topic", "")
        if not topic:
            print("错误: 请提供 topic")
            return 1
        try:
            bd = BlindspotDiscovery()
        except BlindspotAssetSchemaError as exc:
            print(f"盲区数据库需要显式对账: {exc}")
            return 1
        if bd.mark_ignored(topic, asset_id=getattr(args, "asset_id", "")):
            print(f"已忽略盲区: {topic}")
        else:
            print(f"未找到盲区: {topic}")
            return 1
        return 0

    if cmd == "resolve":
        print(
            "错误: 禁止手工/self-signed 关闭知识缺口；请由 Wiki 投影事件携带"
            " exact revision、content hash 和独立 coverage receipt 完成闭环"
        )
        return 1

    if cmd == "cleanup":
        days = getattr(args, "days", 15)
        cutoff = datetime.now() - timedelta(days=days)
        db_path = _get_db_path()
        if not db_path.exists():
            print("暂无盲区数据库")
            return 0

        try:
            bd = BlindspotDiscovery()
            expired = bd.expire_resolved_before(cutoff)
        except BlindspotAssetSchemaError as exc:
            print(f"盲区数据库需要显式对账: {exc}")
            return 1
        print(f"已将 {expired} 条 {days} 天前已解决记录追加为 expired；历史修订保留")
        return 0

    print("可用子命令: list, status, ignore, resolve, cleanup")
    return 0
