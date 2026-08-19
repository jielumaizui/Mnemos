#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wiki Rebuild - 选择性重跑已有 Wiki 页面

功能：
    1. 扫描所有 Wiki 页面，评估可读性评分
    2. 检测手工编辑（对比 mtime 与 frontmatter 蒸馏时间）
    3. 检测 L1 对应（frontmatter 中 来源会话 是否能在 L1 storage 找到）
    4. 筛选低可读性 + 未编辑 + 有 L1 的页面进行重跑
    5. dry-run 模式下只生成报告
    6. 写入模式下先备份原页面，再重新蒸馏

用法：
    python3 -m mnemos_cli wiki rebuild --selective [--dry-run] [--min-readability 60]
"""

import re
import json
import shutil
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from core.config import get_config  # noqa: F401

logger = logging.getLogger(__name__)

WIKI_REBUILD_OPERATION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)


# 复用 wiki_builder 的函数
from core.hephaestus.wiki_builder import (  # noqa: E402
    reconstruct_session,
    score_session,
    _get_wiki_dir,
    _mark_processed,
    _log,
    _link_session_records_to_wiki,
    _ensure_wiki_dirs,
    update_index_md,
    _git_auto_commit,
)
from core.hephaestus.distillation_engine import DistillationEngine  # noqa: E402
from core.frontmatter import parse_frontmatter  # noqa: E402
from core.sync_framework.storage_backend import StorageBackend, create_storage_backend  # noqa: E402

# Constants extracted from magic numbers
COMPUTE_READABILITY_SCORE_META_SCORE = 7


@dataclass
class PageAnalysis:
    """单个 Wiki 页面的分析结果"""

    path: Path
    frontmatter: Dict[str, Any]
    body: str
    readability_score: float = 0.0
    readability_detail: Dict[str, Any] = field(default_factory=dict)
    is_user_edited: bool = False
    edit_detection_reason: str = ""
    has_l1_source: bool = False
    session_id: str = ""
    l1_count: int = 0
    selected_for_rebuild: bool = False
    skip_reason: str = ""


@dataclass
class RebuildResult:
    """重跑结果"""

    page_path: Path
    success: bool
    new_paths: List[str] = field(default_factory=list)
    error: str = ""
    backed_up_to: Optional[Path] = None
    l1_count: int = 0


def _parse_distill_time(fm: Dict[str, Any]) -> Optional[datetime]:
    """从 frontmatter 解析蒸馏时间"""
    for key in ("蒸馏时间", "distilled_at", "distill_time"):
        val = fm.get(key)
        if val:
            try:
                # 支持 '2026-06-04 18:55:17' 和 ISO 格式
                val_str = str(val).replace("'", "").replace('"', "")
                if "T" in val_str:
                    return datetime.fromisoformat(val_str.replace("Z", "+00:00"))
                return datetime.strptime(val_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                logger.warning("[wiki_rebuild] ValueError suppressed", exc_info=True)
    return None


def _parse_quality_score(fm: Dict[str, Any]) -> Optional[float]:
    """从 frontmatter 解析质量分"""
    for key in ("质量分", "quality_score", "quality"):
        val = fm.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                logger.warning("[wiki_rebuild] (ValueError, TypeError) suppressed", exc_info=True)
    return None


def _score_structure(body: str) -> Tuple[float, List[str], List[str]]:
    """计算结构完整性分数（40分）。"""
    structure_score = 0.0
    required_sections = {
        r"##\s*结论": 10,
        r"##\s*怎么用": 10,
        r"##\s*详细内容": 10,
        r"##\s*可信度提示": 10,
    }
    found_sections = []
    for pattern, section_score in required_sections.items():
        if re.search(pattern, body, re.IGNORECASE):
            structure_score += section_score
            found_sections.append(pattern.replace(r"##\s*", ""))
    missing_sections = [
        p.replace(r"##\s*", "") for p in required_sections if p not in found_sections
    ]
    return structure_score, found_sections, missing_sections


def _score_frontmatter(fm: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """计算 frontmatter 质量分数（30分）。"""
    fm_score = 0.0
    detail: Dict[str, Any] = {}

    summary = str(fm.get("摘要", fm.get("summary", ""))).strip()
    if len(summary) >= 50:
        fm_score += 10
    elif len(summary) >= 20:
        fm_score += 5
    detail["summary_length"] = len(summary)

    name = str(fm.get("名称", fm.get("name", fm.get("title", "")))).strip()
    bad_names = {"untitled", "未命名", "", "无", "unknown"}
    name_valid = name and name.lower() not in bad_names
    if name_valid:
        fm_score += 8
    detail["name_valid"] = name_valid

    key_fields = {
        "领域": 3,
        "domain": 3,
        "证据级别": 3,
        "evidence_level": 3,
        "置信度": 3,
        "confidence": 3,
        "时效性": 3,
        "temporal_scope": 3,
    }
    present_fields = []
    for field_key, field_score in key_fields.items():
        if field_key in fm and fm[field_key]:
            fm_score += field_score
            present_fields.append(field_key)
    detail["present_fields"] = list(set(present_fields))

    return fm_score, detail


def _score_meta(fm: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """计算蒸馏元数据分数（30分）。"""
    meta_score = 0.0
    detail: Dict[str, Any] = {}

    quality = _parse_quality_score(fm)
    if quality is not None:
        if quality >= 60:
            meta_score += 15
        elif quality >= 40:
            meta_score += 8
        detail["quality_score"] = quality
    else:
        detail["quality_score"] = None

    from core.hephaestus.distillation_prompts import PROMPT_VERSION

    pv = fm.get("distill_prompt_version") or fm.get("prompt_version")
    if pv and str(pv) == str(PROMPT_VERSION):
        meta_score += 8
    detail["prompt_version_current"] = pv == PROMPT_VERSION if pv else False

    truncated = fm.get("truncated") is True or fm.get("截断") is True
    if not truncated:
        meta_score += COMPUTE_READABILITY_SCORE_META_SCORE
    detail["truncated"] = truncated

    return meta_score, detail


def _compute_readability_score(fm: Dict[str, Any], body: str) -> Tuple[float, Dict[str, Any]]:
    """
    计算 Wiki 页面可读性评分 (0-100)

    评分维度：
    1. 结构完整性 (40分): 关键章节是否存在
    2. Frontmatter 质量 (30分): 摘要、名称等字段的完整度
    3. 蒸馏元数据 (30分): 质量分、prompt_version、截断标记
    """
    structure_score, found_sections, missing_sections = _score_structure(body)
    fm_score, fm_detail = _score_frontmatter(fm)
    meta_score, meta_detail = _score_meta(fm)

    detail: Dict[str, Any] = {
        "structure_score": round(structure_score, 1),
        "found_sections": found_sections,
        "missing_sections": missing_sections,
        "summary_length": fm_detail["summary_length"],
        "name_valid": fm_detail["name_valid"],
        "present_fields": fm_detail["present_fields"],
        "fm_score": round(fm_score, 1),
        "quality_score": meta_detail["quality_score"],
        "prompt_version_current": meta_detail["prompt_version_current"],
        "truncated": meta_detail["truncated"],
        "meta_score": round(meta_score, 1),
    }

    total_score = structure_score + fm_score + meta_score
    detail["total_score"] = round(total_score, 1)
    return total_score, detail


def _is_user_edited(file_path: Path, fm: Dict[str, Any]) -> Tuple[bool, str]:
    """
    检测页面是否被用户手工编辑过

    检测方法（按优先级）：
    1. frontmatter 中显式标记 user_edited: true
    2. 文件 mtime 明显晚于蒸馏时间（>1小时）
    """
    # 方法1: 显式标记
    if fm.get("user_edited") is True or fm.get("手工编辑") is True:
        return True, "frontmatter 显式标记 user_edited"

    # 方法2: mtime 对比
    distill_time = _parse_distill_time(fm)
    if distill_time is None:
        # 无法判断，保守认为未编辑（但记录原因）
        return False, "无法解析蒸馏时间，默认未编辑"

    try:
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
        # 如果 mtime 比蒸馏时间晚超过 1 小时，认为被编辑过
        if mtime > distill_time + timedelta(hours=1):
            delta = mtime - distill_time
            return (
                True,
                f"文件 mtime ({mtime.isoformat()}) 晚于蒸馏时间 ({distill_time.isoformat()}) {delta}",
            )
    except (OSError, ValueError, TypeError) as e:
        return False, f"mtime 检查失败: {e}"

    return False, "未检测到编辑迹象"


def _get_session_id_from_fm(fm: Dict[str, Any]) -> str:
    """从 frontmatter 提取 session_id"""
    for key in ("来源会话", "source_session", "session_id"):
        val = fm.get(key)
        if val:
            return str(val).strip()
    return ""


def _fetch_l1_for_session(session_id: str, backend: StorageBackend) -> List[Dict]:
    """从 StorageBackend 获取特定 session 的所有 L1 记录"""
    if not session_id:
        return []
    try:
        results = backend.list_by_tags(["layer=L1", f"session={session_id}"])
        return [
            {
                "uid": r.uid,
                "content": r.content,
                "tags": r.tags,
                "createTime": r.created_at or "",
                "updateTime": r.updated_at or "",
            }
            for r in results
        ]
    except WIKI_REBUILD_OPERATION_ERRORS as e:
        logger.warning("[WikiRebuild] 查询 session=%s 的 L1 记录失败: %s", session_id, e)
        return []


def analyze_page(file_path: Path) -> Optional[PageAnalysis]:
    """分析单个 Wiki 页面"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, IOError) as e:
        logger.debug("无法读取 %s: %s", file_path, e, exc_info=True)
        return None

    fm, body = parse_frontmatter(content)
    if fm is None:
        return None  # 跳过无 frontmatter 的文件

    # 只处理蒸馏生成的页面（有 source_session 或 蒸馏时间）
    session_id = _get_session_id_from_fm(fm)
    has_distill_marker = bool(
        session_id
        or fm.get("蒸馏时间")
        or fm.get("distilled_at")
        or fm.get("来源") in ("claude", "kimi", "codex", "openclaw", "hermes", "opencode")
    )
    if not has_distill_marker:
        return None

    readability_score, readability_detail = _compute_readability_score(fm, body)
    is_edited, edit_reason = _is_user_edited(file_path, fm)

    return PageAnalysis(
        path=file_path,
        frontmatter=fm,
        body=body,
        readability_score=readability_score,
        readability_detail=readability_detail,
        is_user_edited=is_edited,
        edit_detection_reason=edit_reason,
        session_id=session_id,
    )


def scan_wiki_pages(wiki_dir: Path) -> List[PageAnalysis]:
    """扫描 Wiki 目录下所有 Markdown 页面并分析"""
    results = []  # type: ignore[var-annotated]
    system_pages = {"log.md", "index.md", "graph-index.md", "readme.md"}

    if not wiki_dir.exists():
        return results

    for md_file in wiki_dir.rglob("*.md"):
        if md_file.name.lower() in system_pages:
            continue
        analysis = analyze_page(md_file)
        if analysis:
            results.append(analysis)

    return results


def filter_pages_for_rebuild(
    analyses: List[PageAnalysis],
    min_readability: float = 60.0,
    include_edited: bool = False,
) -> List[PageAnalysis]:
    """筛选需要重跑的页面"""
    selected = []
    for a in analyses:
        reasons = []

        if not a.session_id:
            reasons.append("无来源会话(session_id)")
        else:
            a.has_l1_source = True  # 标记为有 L1 来源，实际 L1 storage 查询在重跑时进行

        if a.is_user_edited and not include_edited:
            reasons.append(f"手工编辑 ({a.edit_detection_reason})")

        if a.readability_score >= min_readability:
            reasons.append(f"可读性评分 {a.readability_score:.1f} >= 门槛 {min_readability}")

        if reasons:
            a.skip_reason = "; ".join(reasons)
            a.selected_for_rebuild = False
        else:
            a.selected_for_rebuild = True
            selected.append(a)

    return selected


def _populate_l1_counts(analyses: List[PageAnalysis], backend: Optional[StorageBackend]) -> None:
    """Populate known L1 record counts for report provenance."""
    if backend is None:
        return
    for analysis in analyses:
        if not analysis.session_id:
            continue
        records = _fetch_l1_for_session(analysis.session_id, backend)
        analysis.l1_count = len(records)
        analysis.has_l1_source = analysis.l1_count > 0


def generate_dry_run_report(
    all_analyses: List[PageAnalysis],
    selected: List[PageAnalysis],
    min_readability: float,
) -> str:
    """生成 dry-run 报告"""
    lines = [
        "# Wiki 选择性重跑 — Dry Run 报告",
        "",
        f"生成时间: {datetime.now().isoformat()}",
        f"可读性门槛: {min_readability}",
        f"总页面数: {len(all_analyses)}",
        f"选中重跑: {len(selected)}",
        f"跳过: {len(all_analyses) - len(selected)}",
        "",
        "## 选中重跑的页面",
        "",
    ]

    for a in sorted(selected, key=lambda x: x.readability_score):
        rel_path = a.path.relative_to(_get_wiki_dir())
        lines.append(
            f"- `{rel_path}` | 可读性: {a.readability_score:.1f} | L1 records: {a.l1_count} | session: `{a.session_id[:16] if a.session_id else 'N/A'}`..."  # noqa: E501
        )
        detail = a.readability_detail
        missing = detail.get("missing_sections", [])
        if missing:
            lines.append(f"  - 缺失章节: {', '.join(missing)}")
        if detail.get("truncated"):
            lines.append("  - ⚠️ 来源曾被截断")
        qs = detail.get("quality_score")
        if qs is not None and qs < 50:
            lines.append(f"  - 质量分较低: {qs:.1f}")
        lines.append("")

    lines.extend(
        [
            "",
            "## 跳过的页面（前20）",
            "",
        ]
    )
    skipped = [a for a in all_analyses if not a.selected_for_rebuild]
    for a in skipped[:20]:
        rel_path = a.path.relative_to(_get_wiki_dir())
        lines.append(f"- `{rel_path}` | 可读性: {a.readability_score:.1f} | 原因: {a.skip_reason}")

    if len(skipped) > 20:
        lines.append(f"- ... 还有 {len(skipped) - 20} 个")

    return "\n".join(lines)


def backup_page(page_path: Path, backup_dir: Path) -> Path:
    """备份单个页面"""
    wiki_dir = _get_wiki_dir()
    rel_path = page_path.relative_to(wiki_dir)
    backup_path = backup_dir / rel_path
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(page_path, backup_path)
    return backup_path


def rebuild_single_page(
    analysis: PageAnalysis,
    backend: StorageBackend,
    engine: DistillationEngine,
    dry_run: bool = False,
    backup_dir: Optional[Path] = None,
) -> RebuildResult:
    """重跑单个页面"""
    result = RebuildResult(page_path=analysis.path, success=False)

    if dry_run:
        result.success = True  # dry-run 标记为"可处理"
        result.l1_count = analysis.l1_count
        return result

    # 1. 备份
    if backup_dir:
        try:
            result.backed_up_to = backup_page(analysis.path, backup_dir)
        except OSError as e:
            result.error = f"备份失败: {e}"
            return result

    # 2. 从 StorageBackend 获取 L1 记录
    l1_records = _fetch_l1_for_session(analysis.session_id, backend)
    if not l1_records:
        result.error = f"找不到 session={analysis.session_id} 的 L1 记录"
        return result
    result.l1_count = len(l1_records)

    # 3. 重建会话
    messages, meta = reconstruct_session(l1_records)
    if not messages:
        result.error = "重建会话失败：无有效消息"
        return result

    # 4. 质量评分
    avg_score, score_detail = score_session(messages)

    # 5. 重新蒸馏
    try:
        distill_result = engine.process(analysis.session_id, messages, meta)

        if distill_result.judgment == "knowledge" and distill_result.fragments:
            # 先删除旧页面（避免重复）
            # 注意：只删除当前这个文件，不删除同 session 的其他文件
            # 因为可能有多个知识形态生成了多个页面
            analysis.path.unlink(missing_ok=True)

            written = engine.write_pages(distill_result)
            result.new_paths = written
            result.success = True

            # 标记处理状态
            _mark_processed(
                analysis.session_id,
                meta.get("source", "unknown"),
                len(messages),
                avg_score,
                ",".join(written) if written else "",
                method="pipeline",
            )
            _link_session_records_to_wiki(l1_records, written)
            _log(analysis.session_id, "rebuild", f"重跑完成，新路径: {written}")
        else:
            result.error = (
                f"蒸馏判断: {distill_result.judgment}, 原因: {distill_result.judgment_reason}"
            )
    except WIKI_REBUILD_OPERATION_ERRORS as e:
        result.error = f"蒸馏失败: {e}"
        logger.warning("[WikiRebuild] 重跑 %s 失败: %s", analysis.path, e, exc_info=True)

    return result


def run_selective_rebuild(
    dry_run: bool = False,
    min_readability: float = 60.0,
    include_edited: bool = False,
    backup_dir: Optional[Path] = None,
    backend: Optional[StorageBackend] = None,
) -> Dict[str, Any]:
    """
    执行选择性重跑的主入口

    Returns:
        dict with keys: total_scanned, selected, success, failed, dry_run, report_path, l1_total
    """
    _ensure_wiki_dirs()
    wiki_dir = _get_wiki_dir()

    # 1. 扫描所有页面
    print(f"[WikiRebuild] 扫描 Wiki 目录: {wiki_dir}")
    all_analyses = scan_wiki_pages(wiki_dir)
    print(f"[WikiRebuild] 扫描完成: {len(all_analyses)} 个蒸馏页面")

    # 2. 筛选
    selected = filter_pages_for_rebuild(
        all_analyses, min_readability=min_readability, include_edited=include_edited
    )
    print(f"[WikiRebuild] 选中重跑: {len(selected)} 个 (门槛={min_readability})")

    # 3. 如果没有选中，直接返回报告
    if not selected:
        report = generate_dry_run_report(all_analyses, selected, min_readability)
        report_path = wiki_dir / ".rebuild-reports"
        report_path.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_file = report_path / f"dryrun_{ts}.md"
        report_file.write_text(report, encoding="utf-8")
        print(f"[WikiRebuild] 报告已保存: {report_file}")
        return {
            "total_scanned": len(all_analyses),
            "selected": 0,
            "success": 0,
            "failed": 0,
            "dry_run": dry_run,
            "report_path": str(report_file),
            "l1_total": 0,
        }

    if backend is None:
        try:
            backend = create_storage_backend()
        except WIKI_REBUILD_OPERATION_ERRORS as e:
            if dry_run:
                logger.warning("[WikiRebuild] dry-run L1 计数不可用: %s", e, exc_info=True)
            else:
                return {"error": f"无法创建 StorageBackend: {e}"}

    _populate_l1_counts(selected, backend)
    l1_total = sum(a.l1_count for a in selected)

    # 4. dry-run 模式下只生成报告
    if dry_run:
        report = generate_dry_run_report(all_analyses, selected, min_readability)
        report_path = wiki_dir / ".rebuild-reports"
        report_path.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_file = report_path / f"dryrun_{ts}.md"
        report_file.write_text(report, encoding="utf-8")
        print(f"[WikiRebuild] Dry-run 报告已保存: {report_file}")
        return {
            "total_scanned": len(all_analyses),
            "selected": len(selected),
            "success": 0,
            "failed": 0,
            "dry_run": True,
            "report_path": str(report_file),
            "l1_total": l1_total,
        }

    # 5. 初始化 StorageBackend 和蒸馏引擎
    if backend is None:
        return {"error": "无法创建 StorageBackend"}
    engine = DistillationEngine()

    # 6. 设置备份目录
    if backup_dir is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = wiki_dir / ".rebuild-backup" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)
    print(f"[WikiRebuild] 备份目录: {backup_dir}")

    # 7. 执行重跑
    success_count = 0
    failed_count = 0
    results = []

    for i, analysis in enumerate(selected, 1):
        rel_path = analysis.path.relative_to(wiki_dir)
        print(
            f"[WikiRebuild] ({i}/{len(selected)}) 重跑: {rel_path} "
            f"(可读性={analysis.readability_detail.get('total_score', 0):.1f})"
        )

        result = rebuild_single_page(
            analysis,
            backend,
            engine,
            dry_run=False,
            backup_dir=backup_dir,
        )
        results.append(result)

        if result.success:
            success_count += 1
            print(f"  ✓ 成功 -> {result.new_paths}")
        else:
            failed_count += 1
            print(f"  ✗ 失败: {result.error}")

    # 8. 更新索引和 Git
    if success_count > 0:
        update_index_md()
        _git_auto_commit()

    # 9. 生成结果报告
    report_lines = [
        "# Wiki 选择性重跑 — 执行报告",
        "",
        f"执行时间: {datetime.now().isoformat()}",
        f"总扫描: {len(all_analyses)}",
        f"选中重跑: {len(selected)}",
        f"成功: {success_count}",
        f"失败: {failed_count}",
        f"备份目录: {backup_dir}",
        "",
        "## 成功",
        "",
    ]
    for r in results:
        if r.success:
            rel = r.page_path.relative_to(wiki_dir)
            report_lines.append(f"- `{rel}` -> {r.new_paths} | L1 records: {r.l1_count}")
    report_lines.extend(["", "## 失败", ""])
    for r in results:
        if not r.success:
            rel = r.page_path.relative_to(wiki_dir)
            report_lines.append(f"- `{rel}`: {r.error} | L1 records: {r.l1_count}")

    report_path = wiki_dir / ".rebuild-reports"
    report_path.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_file = report_path / f"result_{ts}.md"
    report_file.write_text("\n".join(report_lines), encoding="utf-8")

    print(f"[WikiRebuild] 完成: 成功={success_count}, 失败={failed_count}")
    print(f"[WikiRebuild] 报告: {report_file}")

    return {
        "total_scanned": len(all_analyses),
        "selected": len(selected),
        "success": success_count,
        "failed": failed_count,
        "dry_run": False,
        "backup_dir": str(backup_dir),
        "report_path": str(report_file),
        "l1_total": sum(r.l1_count for r in results),
    }


# ========== CLI 入口 ==========


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Wiki 选择性重跑")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告，不写入")
    parser.add_argument(
        "--min-readability", type=float, default=60.0, help="可读性评分门槛 (默认 60)"
    )
    parser.add_argument("--include-edited", action="store_true", help="包含已被用户手工编辑的页面")
    parser.add_argument(
        "--backup-dir",
        type=str,
        default="",
        help="备份目录 (默认: wiki/.rebuild-backup/YYYYMMDD-HHMMSS)",
    )
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    result = run_selective_rebuild(
        dry_run=args.dry_run,
        min_readability=args.min_readability,
        include_edited=args.include_edited,
        backup_dir=backup_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
