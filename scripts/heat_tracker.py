#!/usr/bin/env python3
"""
Heat Tracker - 知识库可视化报告生成器

生成 HTML 报告展示知识库的热力、分布、趋势和关联网络。

用法：
    python3 scripts/heat_tracker.py              # 生成报告到 wiki/.kg/heat_report.html
    python3 scripts/heat_tracker.py --open       # 生成后自动打开浏览器
    python3 scripts/heat_tracker.py --output ~/Desktop/wiki_heat.html
"""

import os
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config

WIKI_DIR = get_config().wiki_dir
OUTPUT_DEFAULT = WIKI_DIR / ".kg" / "heat_report.html"


def collect_wiki_stats() -> dict:
    """收集 Wiki 统计数据"""
    stats = {
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

    # 统计各目录文件数
    dir_counts = {}
    for subdir in ["00-Inbox", "01-People", "02-Projects", "03-Tech",
                   "04-Concepts", "05-MOCs", "retrospectives"]:
        path = WIKI_DIR / subdir
        if path.exists():
            md_files = list(path.rglob("*.md"))
            dir_counts[subdir] = len(md_files)
            stats["total_pages"] += len(md_files)

    stats["domain_distribution"] = dir_counts

    # 尝试从知识图谱数据库获取实体和关系数
    graph_db = WIKI_DIR / ".kg" / "graph.db"
    if graph_db.exists():
        try:
            with sqlite3.connect(str(graph_db), timeout=10) as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM entities")
                stats["total_entities"] = cursor.fetchone()[0]
                cursor = conn.execute("SELECT COUNT(*) FROM relations")
                stats["total_relations"] = cursor.fetchone()[0]
        except Exception:
            pass

    # 从 wiki 页面 frontmatter 提取类型和热力数据
    heat_scores = []
    type_counts = Counter()
    recent_pages = []

    for md_file in WIKI_DIR.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            frontmatter = {}

            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        import yaml
                        frontmatter = yaml.safe_load(parts[1]) or {}
                    except Exception:
                        pass

            # 类型统计
            page_type = frontmatter.get("type", "unknown")
            if page_type:
                type_counts[page_type] += 1

            # 热力分数
            heat = frontmatter.get("heat", frontmatter.get("freshness_score", 0))
            if heat:
                heat_scores.append({
                    "page": str(md_file.relative_to(WIKI_DIR)),
                    "heat": float(heat) if isinstance(heat, (int, float)) else 0.5,
                    "title": frontmatter.get("title", md_file.stem),
                })

            # 最近更新
            updated = frontmatter.get("updated", "")
            if updated:
                try:
                    updated_date = datetime.strptime(updated, "%Y-%m-%d")
                    days_ago = (datetime.now() - updated_date).days
                    if days_ago <= 30:
                        recent_pages.append({
                            "page": str(md_file.relative_to(WIKI_DIR)),
                            "title": frontmatter.get("title", md_file.stem),
                            "updated": updated,
                            "days_ago": days_ago,
                            "heat": frontmatter.get("heat", 0.5),
                        })
                except Exception:
                    pass
        except Exception:
            pass

    stats["type_distribution"] = dict(type_counts)
    stats["heat_scores"] = sorted(heat_scores, key=lambda x: x["heat"], reverse=True)[:50]
    stats["recent_pages"] = sorted(recent_pages, key=lambda x: x["days_ago"])[:20]

    # 从 DNA 数据库获取高频实体
    dna_db = WIKI_DIR / ".kg" / "dna.db"
    if dna_db.exists():
        try:
            with sqlite3.connect(str(dna_db), timeout=10) as conn:
                cursor = conn.execute("""
                    SELECT page_path, keywords FROM knowledge_dna
                    ORDER BY created_at DESC LIMIT 50
                """)
                entity_counts = Counter()
                for row in cursor.fetchall():
                    keywords = row[1]
                    if keywords:
                        for kw in keywords.split(","):
                            entity_counts[kw.strip()] += 1
                stats["top_entities"] = entity_counts.most_common(20)
        except Exception:
            pass

    return stats


def generate_html(stats: dict) -> str:
    """生成 HTML 报告"""

    # 准备图表数据
    domain_labels = list(stats["domain_distribution"].keys())
    domain_values = list(stats["domain_distribution"].values())

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
    <title>Memos-Wiki Heat Tracker</title>
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
        <h1>🔥 Memos-Wiki Heat Tracker</h1>
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
        <p>Memos-Wiki v6.0 | Heat Tracker | 数据实时从 Wiki 目录采集</p>
    </div>

    <script>
        // 领域分布饼图
        echarts.init(document.getElementById('domainChart')).setOption({{
            tooltip: {{ trigger: 'item' }},
            series: [{{
                type: 'pie',
                radius: ['40%', '70%'],
                data: {json.dumps([{"name": k, "value": v} for k, v in stats['domain_distribution'].items()])},
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
            xAxis: {{ type: 'category', data: {json.dumps(type_labels)}, axisLabel: {{ rotate: 30 }} }},
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
            xAxis: {{ type: 'category', data: {json.dumps(entity_names[:15])}, axisLabel: {{ rotate: 30 }} }},
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
    parser.add_argument("--output", default=str(OUTPUT_DEFAULT),
                        help=f"输出 HTML 路径（默认: {OUTPUT_DEFAULT}）")
    parser.add_argument("--open", action="store_true",
                        help="生成后自动用浏览器打开")
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
            subprocess.run(["start", str(output_path)], shell=True)
        else:
            subprocess.run(["xdg-open", str(output_path)])


if __name__ == "__main__":
    main()
