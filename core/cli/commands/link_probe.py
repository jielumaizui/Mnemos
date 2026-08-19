"""Link probe command for Mnemos CLI."""

import logging

from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def cmd_link_probe(args):
    """外部链接探测 CLI 入口。"""
    cfg = _get_config()
    if not cfg.get("features.enable_link_probe", False):
        print("链接探测功能未启用。请在配置中设置 features.enable_link_probe = true。")
        return 1

    from core.hephaestus.link_probe_worker import LinkProbeWorker

    worker = LinkProbeWorker()

    if args.link_probe_cmd == "run":
        batch = worker.probe_batch(batch_size=args.batch_size)
        broken = [item for item in batch if item.get("status") == "broken"]
        updated_pages = set()
        for item in batch:
            page_path = item.get("page_path")
            if page_path and worker.update_wiki_frontmatter(page_path):
                updated_pages.add(page_path)
        print(
            f"探测完成: {len(batch)} 个链接, {len(broken)} 个失效, {len(updated_pages)} 个页面已更新 frontmatter"
        )
        for item in broken:
            print(f"  [BROKEN] {item.get('url')} (页面: {item.get('page_path')})")
        return 0

    if args.link_probe_cmd == "status":
        import sqlite3

        db_path = worker.db_path
        if not db_path.exists():
            print("链接探测队列为空（数据库尚未创建）。")
            return 0
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) FROM link_probe_queue GROUP BY status")
            rows = cursor.fetchall()
        total = sum(count for _, count in rows)
        print(f"链接探测队列: {total} 条")
        for status, count in rows:
            print(f"  {status}: {count}")
        return 0

    print("用法: mnemos link-probe {run|status}")
    return 1
