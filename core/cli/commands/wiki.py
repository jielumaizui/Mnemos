"""Wiki command for Mnemos CLI."""

import logging
from pathlib import Path

from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def _print_page_metadata(result: dict, page_path: str) -> None:
    """打印 Wiki 页面元数据。"""
    print(f"📄 {result.get('title', page_path)}")
    print(f"   深度: {result.get('depth', 'unknown')}")
    if result.get("confidence"):
        print(f"   可信度: {result['confidence']}")
    if result.get("verification"):
        print(f"   验证状态: {result['verification']}")
    if result.get("source"):
        print(f"   来源: {result['source']}")
    if result.get("last_modified"):
        print(f"   最后更新: {result['last_modified']}")


def _print_related_pages(related: list) -> None:
    """打印关联页面列表。"""
    print(f"\n🔗 关联页面 ({len(related)} 个):")
    for rel in related[:5]:
        label = rel.get("title") or rel.get("path") or rel.get("page_id") or "unknown"
        relation = rel.get("relation")
        print(f"   - {label}{f' ({relation})' if relation else ''}")


def _cmd_wiki_read(args) -> None:
    """读取并打印 Wiki 页面。"""
    try:
        from integrations.oracle import WikiReader

        reader = WikiReader()
        result = reader.read_page(args.page_path, depth=args.depth)
        if not result:
            print(f"未找到页面: {args.page_path}")
            return
        _print_page_metadata(result, args.page_path)
        print("-" * 40)
        content = result.get("content") or result.get("summary") or ""
        if content:
            print(content[:2000])
        if result.get("related"):
            _print_related_pages(result["related"])
    except (ImportError, AttributeError, OSError) as e:
        print(f"读取 Wiki 失败: {e}")


def _resolve_backup_dir(backup_dir_raw: str | None) -> Path | None:
    """校验并返回合法的备份目录，非法时打印错误并返回 None。"""
    if not backup_dir_raw or not backup_dir_raw.strip():
        return None
    backup_dir = Path(backup_dir_raw).expanduser().resolve()
    wiki_dir = _get_config().wiki_dir.resolve()
    if backup_dir == wiki_dir or wiki_dir in backup_dir.parents:
        print(f"错误: 备份目录不能位于 Wiki 目录内部: {backup_dir}")
        return None
    if backup_dir.exists() and not backup_dir.is_dir():
        print(f"错误: 备份路径已存在但不是目录: {backup_dir}")
        return None
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"错误: 无法创建备份目录 {backup_dir}: {e}")
        return None
    return backup_dir


def _print_rebuild_result(result: dict) -> None:
    """打印 Wiki 重建结果。"""
    if "error" in result:
        print(f"错误: {result['error']}")
        return
    print("\n=== Wiki 重跑结果 ===")
    print(f"  总扫描: {result['total_scanned']}")
    print(f"  选中: {result['selected']}")
    print(f"  成功: {result.get('success', 0)}")
    print(f"  失败: {result.get('failed', 0)}")
    if result.get("dry_run"):
        print("  模式: dry-run")
    if result.get("backup_dir"):
        print(f"  备份: {result['backup_dir']}")
    print(f"  报告: {result.get('report_path', 'N/A')}")


def _cmd_wiki_rebuild(args) -> None:
    """执行 Wiki 选择性重建。"""
    try:
        from core.hephaestus.wiki_rebuild import run_selective_rebuild

        backup_dir = _resolve_backup_dir(getattr(args, "backup_dir", None))
        if backup_dir is None and getattr(args, "backup_dir", None):
            return

        result = run_selective_rebuild(
            dry_run=args.dry_run,
            min_readability=args.min_readability,
            include_edited=args.include_edited,
            backup_dir=backup_dir,
        )
        _print_rebuild_result(result)
    except (ImportError, AttributeError, OSError) as e:
        print(f"Wiki 重跑失败: {e}")
        import traceback

        traceback.print_exc()


def cmd_wiki(args):
    """Wiki 知识库操作"""
    handlers = {
        "read": _cmd_wiki_read,
        "rebuild": _cmd_wiki_rebuild,
    }
    handler = handlers.get(args.wiki_cmd)
    if handler is None:
        print("用法: mnemos wiki read <page_path> [--depth metadata|summary|full]")
        print("       mnemos wiki rebuild --selective [--dry-run] [--min-readability 60]")
        return
    handler(args)
