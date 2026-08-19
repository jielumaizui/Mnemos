#!/usr/bin/env python3
"""
Heat Tracker - 知识库可视化报告生成器

生成 HTML 报告展示知识库的热力、分布、趋势和关联网络。

用法：
    python3 scripts/heat_tracker.py              # 生成报告到 wiki/.kg/heat_report.html
    python3 scripts/heat_tracker.py --open       # 生成后自动打开浏览器
    python3 scripts/heat_tracker.py --output ~/Desktop/wiki_heat.html
"""

import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config  # noqa: E402

WIKI_DIR = get_config().wiki_dir
OUTPUT_DEFAULT = WIKI_DIR / ".kg" / "heat_report.html"


def _count_domain_pages(wiki_dir: Path) -> Tuple[int, Dict[str, int]]:
    """统计各目录的 markdown 页面数。"""
    dir_counts: Dict[str, int] = {}
    total_pages = 0
    for subdir in [
        "00-Inbox",
        "01-People",
        "02-Projects",
        "03-Tech",
        "04-Concepts",
        "05-MOCs",
        "retrospectives",
    ]:
        path = wiki_dir / subdir
        if path.exists():
            md_files = list(path.rglob("*.md"))
            dir_counts[subdir] = len(md_files)
            total_pages += len(md_files)
    return total_pages, dir_counts


def _read_graph_counts(graph_db: Path) -> Tuple[int, int]:
    """从知识图谱数据库读取实体和关系数。"""
    total_entities = 0
    total_relations = 0
    if graph_db.exists():
        try:
            with sqlite3.connect(str(graph_db), timeout=10) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM entities")
                total_entities = cursor.fetchone()[0]
                cursor = conn.execute("SELECT COUNT(*) FROM relations")
                total_relations = cursor.fetchone()[0]
        except (sqlite3.Error, OSError):
            pass
    return total_entities, total_relations


def _scan_page_frontmatter(md_file: Path, wiki_dir: Path) -> Optional[Dict[str, Any]]:
    """读取单个 wiki 页面的 frontmatter，提取类型、热力、最近更新信息。"""
    try:
        content = md_file.read_text(encoding="utf-8")
    except ValueError:
        return None

    frontmatter: Dict[str, Any] = {}
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml

                frontmatter = yaml.safe_load(parts[1]) or {}
            except ImportError:
                pass

    page_type = frontmatter.get("type", "unknown")
    heat = frontmatter.get("heat", frontmatter.get("freshness_score", 0))
    title = frontmatter.get("title", md_file.stem)
    rel_path = str(md_file.relative_to(wiki_dir))

    heat_entry: Optional[Dict[str, Any]] = None
    if heat:
        heat_entry = {
            "page": rel_path,
            "heat": float(heat) if isinstance(heat, (int, float)) else 0.5,
            "title": title,
        }

    recent_entry: Optional[Dict[str, Any]] = None
    updated = frontmatter.get("updated", "")
    if updated:
        try:
            updated_date = datetime.strptime(updated, "%Y-%m-%d")
            days_ago = (datetime.now() - updated_date).days
            if days_ago <= 30:
                recent_entry = {
                    "page": rel_path,
                    "title": title,
                    "updated": updated,
                    "days_ago": days_ago,
                    "heat": frontmatter.get("heat", 0.5),
                }
        except ValueError:
            pass

    return {
        "type": page_type,
        "heat_entry": heat_entry,
        "recent_entry": recent_entry,
    }


def _collect_heat_and_recent(wiki_dir: Path) -> Tuple[Dict[str, int], List[Dict], List[Dict]]:
    """遍历 wiki 页面，收集类型分布、热力分数、最近更新页面。"""
    heat_scores: List[Dict] = []
    type_counts: Counter = Counter()
    recent_pages: List[Dict] = []

    for md_file in wiki_dir.rglob("*.md"):
        page_info = _scan_page_frontmatter(md_file, wiki_dir)
        if page_info is None:
            continue

        page_type = page_info["type"]
        if page_type:
            type_counts[page_type] += 1

        heat_entry = page_info["heat_entry"]
        if heat_entry:
            heat_scores.append(heat_entry)

        recent_entry = page_info["recent_entry"]
        if recent_entry:
            recent_pages.append(recent_entry)

    type_distribution = dict(type_counts)
    heat_scores = sorted(heat_scores, key=lambda x: x["heat"], reverse=True)[:50]
    recent_pages = sorted(recent_pages, key=lambda x: x["days_ago"])[:20]
    return type_distribution, heat_scores, recent_pages


def _read_top_entities(dna_db: Path) -> List[Tuple[str, int]]:
    """从 DNA 数据库读取高频实体。"""
    if not dna_db.exists():
        return []
    try:
        with sqlite3.connect(str(dna_db), timeout=10) as conn:
            cursor = conn.execute("""
                SELECT page_path, keywords FROM knowledge_dna
                ORDER BY created_at DESC LIMIT 50
            """)
            entity_counts: Counter = Counter()
            for row in cursor.fetchall():
                keywords = row[1]
                if keywords:
                    for kw in keywords.split(","):
                        entity_counts[kw.strip()] += 1
            return entity_counts.most_common(20)
    except (sqlite3.Error, OSError):
        return []


def collect_wiki_stats() -> dict:
    """收集 Wiki 统计数据"""
    stats: Dict[str, Any] = {
        "total_pages": 0,
        "total_entities": 0,
        "total_relations": 0,
        "domain_distribution": {},
        "type_distribution": {},
        "heat_scores": [],
        "recent_pages": [],
        "top_entities": [],
    }

    if not WIKI_DIR.exists():
        return stats

    stats["total_pages"], stats["domain_distribution"] = _count_domain_pages(WIKI_DIR)
    stats["total_entities"], stats["total_relations"] = _read_graph_counts(
        WIKI_DIR / ".kg" / "graph.db"
    )
    (
        stats["type_distribution"],
        stats["heat_scores"],
        stats["recent_pages"],
    ) = _collect_heat_and_recent(WIKI_DIR)
    stats["top_entities"] = _read_top_entities(WIKI_DIR / ".kg" / "dna.db")

    return stats


def generate_html(stats: dict) -> str:
    """生成 HTML 报告"""

    # 准备图表数据
    list(stats["domain_distribution"].keys())
    list(stats["domain_distribution"].values())

    type_labels = list(stats["type_distribution"].keys())
    type_values = list(stats["type_distribution"].values())

    heat_pages = [h["page"].split("/")[-1][:20] for h in stats["heat_scores"][:15]]
    heat_values = [h["heat"] for h in stats["heat_scores"][:15]]

    entity_names = [e[0] for e in stats["top_entities"]]
    entity_counts = [e[1] for e in stats["top_entities"]]

    recent_html = ""
    for p in stats["recent_pages"]:
        heat_color = "#52c41a" if p["heat"] >= 0.7 else "#faad14" if p["heat"] >= 0.4 else "#f5222d"
        recent_html += f"""
        <tr>
            <td>{p['title']}</td>
            <td>{p['updated']}</td>
            <td><span style="color:{heat_color};font-weight:bold;">{p['heat']:.2f}</span></td>
            <td>{p['page']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mnemos Heat Tracker</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 20px;
            text-align: center;
        }}
        .header h1 {{ font-size: 2em; margin-bottom: 10px; }}
        .header .meta {{ opacity: 0.9; }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}
        .stat-card {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            text-align: center;
        }}
        .stat-card .number {{
            font-size: 2.5em;
            font-weight: bold;
            color: #667eea;
            margin: 10px 0;
        }}
        .stat-card .label {{ color: #666; font-size: 0.9em; }}
        .chart-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(500px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}
        .chart-card {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }}
        .chart-card h3 {{
            margin-bottom: 15px;
            color: #333;
            font-size: 1.1em;
        }}
        .chart {{
            width: 100%;
            height: 350px;
        }}
        .table-card {{
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            margin: 20px 0;
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9em;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }}
        th {{
            background: #f8f9fa;
            font-weight: 600;
            color: #555;
        }}
        tr:hover {{ background: #f8f9fa; }}
        .footer {{
            text-align: center;
            padding: 40px 20px;
            color: #999;
            font-size: 0.85em;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🔥 Mnemos Heat Tracker</h1>
        <p class="meta">知识库可视化报告 | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
    </div>

    <div class="container">
        <!-- 概览统计 -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="label">总页面数</div>
                <div class="number">{stats['total_pages']}</div>
            </div>
            <div class="stat-card">
                <div class="label">实体数</div>
                <div class="number">{stats['total_entities']}</div>
            </div>
            <div class="stat-card">
                <div class="label">关系数</div>
                <div class="number">{stats['total_relations']}</div>
            </div>
            <div class="stat-card">
                <div class="label">知识类型</div>
                <div class="number">{len(type_labels)}</div>
            </div>
        </div>

        <!-- 图表区域 -->
        <div class="chart-grid">
            <div class="chart-card">
                <h3>📁 领域分布</h3>
                <div id="domainChart" class="chart"></div>
            </div>
            <div class="chart-card">
                <h3>📊 知识类型分布</h3>
                <div id="typeChart" class="chart"></div>
            </div>
            <div class="chart-card">
                <h3>🔥 热力 TOP15</h3>
                <div id="heatChart" class="chart"></div>
            </div>
            <div class="chart-card">
                <h3>🏷️ 高频实体</h3>
                <div id="entityChart" class="chart"></div>
            </div>
        </div>

        <!-- 最近活跃知识 -->
        <div class="table-card">
            <h3>🕐 最近活跃知识（30天内更新）</h3>
            <table>
                <thead>
                    <tr>
                        <th>标题</th>
                        <th>更新日期</th>
                        <th>热力</th>
                        <th>路径</th>
                    </tr>
                </thead>
                <tbody>
                    {recent_html}
                </tbody>
            </table>
        </div>
    </div>

    <div class="footer">
        <p>Mnemos v2.0.0 | Heat Tracker | 数据实时从 Wiki 目录采集</p>
    </div>

    <script>
        // 领域分布饼图
        echarts.init(document.getElementById('domainChart')).setOption({{
            tooltip: {{ trigger: 'item' }},
            series: [{{
                type: 'pie',
                radius: ['40%', '70%'],
                data: {json.dumps([{"name": k, "value": v} for k, v in stats['domain_distribution'].items()])},  # noqa: E501
                emphasis: {{
                    itemStyle: {{
                        shadowBlur: 10,
                        shadowOffsetX: 0,
                        shadowColor: 'rgba(0,0,0,0.5)'
                    }}
                }}
            }}]
        }});

        // 知识类型柱状图
        echarts.init(document.getElementById('typeChart')).setOption({{
            tooltip: {{ trigger: 'axis' }},
            xAxis: {{ type: 'category', data: {json.dumps(type_labels)}, axisLabel: {{ rotate: 30 }} }},  # noqa: E501
            yAxis: {{ type: 'value' }},
            series: [{{
                data: {json.dumps(type_values)},
                type: 'bar',
                itemStyle: {{ color: '#667eea', borderRadius: [4, 4, 0, 0] }}
            }}]
        }});

        // 热力 TOP15 横向柱状图
        echarts.init(document.getElementById('heatChart')).setOption({{
            tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
            xAxis: {{ type: 'value', max: 1 }},
            yAxis: {{ type: 'category', data: {json.dumps(list(reversed(heat_pages)))} }},
            series: [{{
                type: 'bar',
                data: {json.dumps(list(reversed(heat_values)))},
                itemStyle: {{
                    color: function(params) {{
                        var val = params.value;
                        return val >= 0.7 ? '#52c41a' : val >= 0.4 ? '#faad14' : '#f5222d';
                    }},
                    borderRadius: [0, 4, 4, 0]
                }}
            }}]
        }});

        // 高频实体词云图（用柱状图替代）
        echarts.init(document.getElementById('entityChart')).setOption({{
            tooltip: {{ trigger: 'axis' }},
            xAxis: {{ type: 'category', data: {json.dumps(entity_names[:15])}, axisLabel: {{ rotate: 30 }} }},  # noqa: E501
            yAxis: {{ type: 'value' }},
            series: [{{
                data: {json.dumps(entity_counts[:15])},
                type: 'bar',
                itemStyle: {{ color: '#764ba2', borderRadius: [4, 4, 0, 0] }}
            }}]
        }});
    </script>
</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="Heat Tracker - 知识库可视化")
    parser.add_argument(
        "--output", default=str(OUTPUT_DEFAULT), help=f"输出 HTML 路径（默认: {OUTPUT_DEFAULT}）"
    )
    parser.add_argument("--open", action="store_true", help="生成后自动用浏览器打开")
    args = parser.parse_args()

    print("🔥 Heat Tracker - 采集知识库数据...")
    stats = collect_wiki_stats()

    print(f"  总页面: {stats['total_pages']}")
    print(f"  实体数: {stats['total_entities']}")
    print(f"  关系数: {stats['total_relations']}")
    print(f"  知识类型: {len(stats['type_distribution'])}")

    print("📊 生成 HTML 报告...")
    html = generate_html(stats)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    print(f"✅ 报告已生成: {output_path}")

    if args.open:
        import subprocess

        if sys.platform == "darwin":
            subprocess.run(["open", str(output_path)])
        elif sys.platform == "win32":
            subprocess.run(["cmd", "/c", "start", "", str(output_path)])
        else:
            subprocess.run(["xdg-open", str(output_path)])


if __name__ == "__main__":
    main()
