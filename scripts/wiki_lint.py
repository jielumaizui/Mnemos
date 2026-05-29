#!/usr/bin/env python3
"""
Wiki Lint - Karpathy 风格健康扫描脚本

扫描 wiki/ 目录，检测知识健康问题：
1. 孤立页面（orphan）：无入链也无出链
2. 过短页面（stub）：内容 < 200 字符
3. 缺 frontmatter 的页面
4. 坏链接（broken link）：[[xxx]] 指向不存在的页面
5. 过旧页面（stale）：mtime > 30 天
6. 缺元数据（missing meta）：无 status / source_count / knowledge_stage
7. 未引用来源（no sources）：source_count == 0 或 sources 为空

用法:
  python3 scripts/wiki_lint.py           # 扫描并报告
  python3 scripts/wiki_lint.py --fix     # 自动修复简单问题
  python3 scripts/wiki_lint.py --json    # 输出 JSON 报告
"""

import os
import sys
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Set, Tuple, Optional
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config
from core.frontmatter import fm_get, to_chinese_frontmatter

WIKI_DIR = get_config().wiki_dir

FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
WIKI_LINK_RE = re.compile(r'\[\[([^\]]+)\]\]')

# 健康阈值
STUB_THRESHOLD = 200          # 字符数，低于此值为 stub
STALE_DAYS = 30               # 超过此天数为陈旧


def extract_frontmatter(content: str) -> Tuple[Optional[Dict], str]:
    """提取 frontmatter 和正文"""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return None, content
    try:
        import yaml
        return yaml.safe_load(match.group(1)) or {}, content[match.end():]
    except Exception:
        return None, content


def extract_wiki_links(content: str) -> Set[str]:
    """提取 [[...]] 链接的目标页面名"""
    links = set()
    for match in WIKI_LINK_RE.finditer(content):
        link = match.group(1)
        page_name = link.split('|')[0].strip()
        links.add(page_name)
    return links


def scan_all_pages() -> List[Dict]:
    """扫描所有 wiki 页面"""
    pages = []
    if not WIKI_DIR.exists():
        print(f"[Lint] Wiki 目录不存在: {WIKI_DIR}")
        return pages

    # 收集所有 markdown 文件
    md_files = list(WIKI_DIR.rglob("*.md"))
    # 排除隐藏目录
    md_files = [f for f in md_files if not any(p.startswith(".") for p in f.relative_to(WIKI_DIR).parts)]

    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        frontmatter, body = extract_frontmatter(content)
        links = extract_wiki_links(content)
        stat = md_file.stat()

        pages.append({
            "path": str(md_file),
            "rel_path": str(md_file.relative_to(WIKI_DIR)),
            "name": md_file.stem,
            "frontmatter": frontmatter,
            "body": body,
            "content": content,
            "links": links,
            "size": len(content),
            "body_size": len(body.strip()),
            "mtime": datetime.fromtimestamp(stat.st_mtime),
            "line_count": content.count('\n') + 1,
        })

    return pages


def build_page_index(pages: List[Dict]) -> Dict[str, Dict]:
    """建立页面名到页面的索引"""
    index = {}
    for p in pages:
        index[p["name"]] = p
        # 也按相对路径索引（用于链接解析）
        index[p["rel_path"].replace('.md', '')] = p
    return index


def check_orphan(page: Dict, all_pages: List[Dict], page_index: Dict[str, Dict]) -> Tuple[bool, str]:
    """检查是否为孤立页面"""
    # 入链：谁引用了这个页面
    has_incoming = False
    name = page["name"]
    for p in all_pages:
        if name in p["links"]:
            has_incoming = True
            break

    # 出链：这个页面引用了谁
    has_outgoing = len(page["links"]) > 0

    if not has_incoming and not has_outgoing:
        return True, "无入链也无出链"
    if not has_incoming:
        return True, "无入链（无人引用）"
    return False, ""


def check_broken_links(page: Dict, page_index: Dict[str, Dict]) -> List[str]:
    """检查坏链接"""
    broken = []
    for link in page["links"]:
        # 支持多种路径格式
        possible_keys = [
            link,
            link.replace(' ', '_'),
            f"concepts/{link}",
            f"entities/{link}",
            f"sources/{link}",
        ]
        if not any(k in page_index for k in possible_keys):
            broken.append(link)
    return broken


def check_missing_meta(page: Dict) -> List[str]:
    """检查缺失的元数据字段"""
    missing = []
    fm = page["frontmatter"]
    if fm is None:
        missing.append("缺少 frontmatter")
        return missing

    required_fields = ["status", "source_count", "knowledge_stage", "evidence_level"]
    for field in required_fields:
        if fm_get(fm, field) is None:
            missing.append(f"缺少 {field}")

    # source_count 检查
    if fm_get(fm, "source_count", 0) == 0:
        sources = fm.get("sources", fm.get("来源", []))
        if not sources:
            missing.append("source_count 为 0 且无 sources")

    return missing


def lint_page(page: Dict, all_pages: List[Dict], page_index: Dict[str, Dict],
              stale_days: int = 30, stub_threshold: int = 200) -> Dict:
    """对单个页面执行 lint 检查"""
    issues = []
    severity = "ok"  # ok / warning / error

    # 1. 缺少 frontmatter
    if page["frontmatter"] is None:
        issues.append({"type": "no_frontmatter", "msg": "缺少 YAML frontmatter"})
        severity = "error"
    else:
        # 2. 缺元数据
        missing_meta = check_missing_meta(page)
        for m in missing_meta:
            issues.append({"type": "missing_meta", "msg": m})
            if severity == "ok":
                severity = "warning"

    # 3. 过短页面
    if page["body_size"] < stub_threshold:
        issues.append({
            "type": "stub",
            "msg": f"内容过短（{page['body_size']} 字符，阈值 {stub_threshold}）"
        })
        if severity == "ok":
            severity = "warning"

    # 4. 孤立页面
    is_orphan, orphan_reason = check_orphan(page, all_pages, page_index)
    if is_orphan:
        issues.append({"type": "orphan", "msg": orphan_reason})
        if severity == "ok":
            severity = "warning"

    # 5. 坏链接
    broken = check_broken_links(page, page_index)
    for b in broken:
        issues.append({"type": "broken_link", "msg": f"坏链接: [[{b}]]"})
        severity = "error"

    # 6. 过旧页面
    age_days = (datetime.now() - page["mtime"]).days
    if age_days > stale_days:
        issues.append({
            "type": "stale",
            "msg": f"过旧（{age_days} 天未更新，阈值 {stale_days}）"
        })
        if severity == "ok":
            severity = "warning"

    return {
        "page": page["rel_path"],
        "severity": severity,
        "issues": issues,
        "age_days": age_days,
        "body_size": page["body_size"],
    }


def generate_report(results: List[Dict]) -> str:
    """生成 human-readable 报告"""
    total = len(results)
    errors = sum(1 for r in results if r["severity"] == "error")
    warnings = sum(1 for r in results if r["severity"] == "warning")
    ok = sum(1 for r in results if r["severity"] == "ok")

    lines = [
        f"# Wiki Lint 报告",
        f"",
        f"生成时间: {datetime.now().isoformat()}",
        f"总页面: {total}",
        f"  - 健康: {ok}",
        f"  - 警告: {warnings}",
        f"  - 错误: {errors}",
        f"",
    ]

    # 错误优先
    if errors > 0:
        lines.append("## 错误")
        lines.append("")
        for r in results:
            if r["severity"] == "error":
                lines.append(f"- **{r['page']}**")
                for issue in r["issues"]:
                    if issue["type"] in ("no_frontmatter", "broken_link"):
                        lines.append(f"  - {issue['msg']}")
        lines.append("")

    # 警告
    if warnings > 0:
        lines.append("## 警告")
        lines.append("")
        for r in results:
            if r["severity"] == "warning":
                lines.append(f"- **{r['page']}**")
                for issue in r["issues"]:
                    lines.append(f"  - {issue['msg']}")
        lines.append("")

    # 统计摘要
    lines.append("---")
    lines.append("")
    lines.append("## 问题统计")
    lines.append("")

    issue_counts = defaultdict(int)
    for r in results:
        for issue in r["issues"]:
            issue_counts[issue["type"]] += 1

    for issue_type, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- {issue_type}: {count}")

    lines.append("")
    lines.append("## 修复建议")
    lines.append("")
    if errors > 0:
        lines.append("1. **优先修复错误**：补充 frontmatter、修复坏链接")
    if warnings > 0:
        lines.append("2. **处理警告**：扩充 stub 页面、建立页面间链接")
    lines.append("3. **定期运行**: `python3 scripts/wiki_lint.py`")

    return "\n".join(lines)


def auto_fix(results: List[Dict], all_pages: List[Dict], page_index: Dict[str, Dict]) -> int:
    """自动修复简单问题，返回修复数量"""
    fixed = 0
    for r in results:
        page = page_index.get(Path(r["page"]).stem)
        if not page:
            continue

        fm = page.get("frontmatter")
        if fm is None:
            continue

        # 修复缺元数据：写入中文展示字段，内部工具通过 alias 映射读取。
        modified = False
        if fm_get(fm, "knowledge_stage") is None:
            fm["知识阶段"] = "原始"
            modified = True
        if fm_get(fm, "evidence_level") is None:
            fm["证据级别"] = "单源"
            modified = True
        if fm_get(fm, "status") is None:
            fm["状态"] = "草稿"
            modified = True
        if fm_get(fm, "source_count") is None:
            fm["来源数量"] = 1
            modified = True

        if modified:
            # 写回文件
            content = page["content"]
            fm_match = FRONTMATTER_RE.match(content)
            if fm_match:
                fm = to_chinese_frontmatter(fm)
                try:
                    import yaml
                    new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
                except Exception:
                    new_fm = json.dumps(fm, ensure_ascii=False, indent=2)
                new_content = f"---\n{new_fm}\n---\n" + content[fm_match.end():]
                Path(page["path"]).write_text(new_content, encoding="utf-8")
                fixed += 1
                print(f"[Lint] 已修复元数据: {r['page']}")

    return fixed


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Wiki Lint - 健康扫描脚本")
    parser.add_argument("--fix", action="store_true", help="自动修复简单问题")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--stale-days", type=int, default=STALE_DAYS, help=f"陈旧阈值（默认 {STALE_DAYS}）")
    parser.add_argument("--stub-threshold", type=int, default=STUB_THRESHOLD, help=f"stub 阈值（默认 {STUB_THRESHOLD}）")
    args = parser.parse_args()

    stale_days = args.stale_days
    stub_threshold = args.stub_threshold

    print(f"[Lint] 扫描 Wiki 目录: {WIKI_DIR}")
    pages = scan_all_pages()
    print(f"[Lint] 找到 {len(pages)} 个页面")

    if not pages:
        print("[Lint] 没有页面可扫描")
        return

    page_index = build_page_index(pages)
    results = [lint_page(p, pages, page_index, stale_days, stub_threshold) for p in pages]

    # 自动修复
    if args.fix:
        fixed = auto_fix(results, pages, page_index)
        print(f"[Lint] 自动修复了 {fixed} 个页面")
        # 修复后重新扫描
        pages = scan_all_pages()
        page_index = build_page_index(pages)
        results = [lint_page(p, pages, page_index, stale_days, stub_threshold) for p in pages]

    # 输出
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        report = generate_report(results)
        print()
        print(report)

    # 退出码
    errors = sum(1 for r in results if r["severity"] == "error")
    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
