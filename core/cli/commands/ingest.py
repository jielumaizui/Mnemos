"""Ingest command for Mnemos CLI."""

import json
import logging
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def _describe_rejection(reason: str, path: Path) -> str:
    """将 file ingestor 拒绝原因转换为用户可读中文提示。"""
    if reason == "文件不存在":
        return f"路径不存在: {path}"
    if reason == "拒绝摄入符号链接":
        return f"拒绝摄入符号链接（安全风险）: {path}"
    if reason == "无法获取文件状态":
        return f"无法读取文件状态: {path}"
    if reason == "路径为目录，请使用 ingest_directory":
        return f"该路径是目录，请使用 --recursive: {path}"
    if reason == "拒绝摄入非普通文件（设备/管道/socket 等）":
        return f"拒绝摄入特殊文件（设备/管道/socket）: {path}"
    if reason == "拒绝摄入系统临时目录文件":
        return f"拒绝摄入系统临时目录文件: {path}"
    if reason == "拒绝摄入 L1 raw vault 自身文件":
        return f"拒绝摄入 Mnemos raw vault 自身文件: {path}"
    if reason == "无法解析路径（可能已被删除或无权限）":
        return f"无法解析路径（可能已被删除或无权限）: {path}"
    if reason == "无法读取 raw vault 配置，默认拒绝摄入":
        return f"无法读取 Mnemos 配置，默认拒绝摄入: {path}"
    if reason.startswith("文件过大（超过 "):
        return f"{reason}: {path}；配置项 document_process.max_file_size_mb"
    if reason == "无法读取文件大小":
        return f"无法读取文件大小: {path}"
    return f"摄入失败: {path}（{reason}）"


def _print_import_result(result: dict, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if result.get("success"):
        print(result.get("message", "导入完成"))
    else:
        print(result.get("message", "导入失败"))
    print(f"source_path: {result.get('source_path', '')}")
    print(f"source_hash: {result.get('source_hash', '')}")
    print(f"content_size: {result.get('content_size', 0)}")
    print(f"raw_revision_id: {result.get('raw_revision_id') or ''}")
    print(f"queue_id: {result.get('queue_id') or ''}")
    print(f"wiki_paths: {', '.join(result.get('wiki_paths', []))}")
    print(f"quality_decision: {result.get('quality_decision', '')}")
    print(f"routing_result: {result.get('routing_result', {}).get('status', '')}")


def cmd_ingest(args) -> int:
    """摄入本地文件/目录到 canonical raw，并按模式请求异步蒸馏。"""
    mode = getattr(args, "mode", None)
    if mode is not None:
        from core.application.document_import_service import DocumentImportService

        try:
            result = DocumentImportService().import_document(
                args.path,
                mode=mode,
                agent_name=getattr(args, "agent_name", "trusted_user_document"),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        except ValueError as exc:
            print(f"导入参数错误: {exc}")
            return 1
        _print_import_result(result, as_json=bool(getattr(args, "json", False)))
        return 0 if result.get("success") else 1

    from core.sync_framework.file_ingestor import FileIngestor

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"路径不存在: {path}")
        return 1

    # 单文件/目录先做显式安全检查，给出清晰 CLI 反馈
    if path.is_symlink():
        print(f"拒绝摄入符号链接（安全风险）: {path}")
        return 1
    if path.is_file():
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            print(f"拒绝摄入特殊文件（设备/管道/socket）: {path}")
            return 1

    recursive = args.recursive and not args.no_recursive
    ingestor = FileIngestor()

    try:
        if path.is_dir():
            count = ingestor.ingest_directory(path, agent_name=args.agent_name, recursive=recursive)
            print(f"已摄入 {count} 个文件")
            return 0 if count >= 0 else 1
        else:
            saved = ingestor.ingest_file(path, agent_name=args.agent_name)
            if saved:
                print(f"已摄入: {path.name}")
                return 0
            # 尝试给出更具体的原因
            reason = ingestor._validate_file_path(path)
            print(_describe_rejection(reason or "不支持的文件类型或路径", path))
            return 1
    except (OSError, RuntimeError, ValueError, TypeError) as e:
        print(f"摄入异常: {e}")
        return 1
