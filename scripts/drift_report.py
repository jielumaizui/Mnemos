#!/usr/bin/env python3
"""
漂移检测报告生成器

低成本实现：使用 ECharts CDN，无需 matplotlib 依赖。
生成 ~/.mnemos/reports/drift_report.html
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional


def _get_db_path() -> Path:
    from core.config import get_config
    return get_config().data_dir / "mnemos.db"


def _fetch_ground_truth(days: int = 30) -> List[Dict]:
    """读取 ground_truth_signals 表最近 N 天数据"""
    db = _get_db_path()
    if not db.exists():
        return []

    since = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT session_id, signal_type, signal_value, confidence,
                       latency_hours, created_at
                FROM ground_truth_signals
                WHERE created_at > ?
                ORDER BY created_at DESC
                LIMIT 500
                """,
                (since,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _fetch_scorer_status() -> Dict:
    """获取评分器状态"""
    try:
        from core.scoring.scorers.distill_scorer import DistillScorer
        scorer = DistillScorer()
        return scorer._scorer.get_status()
    except Exception as e:
        return {"error": str(e)}


def _aggregate_by_day(records: List[Dict]) -> Dict[str, Dict[str, List[float]]]:
    """按天聚合各 signal_type 的 confidence"""
    by_day: Dict[str, Dict[str, List[float]]] = {}
    for r in records:
        day = r.get("created_at", "")[:10]
        sig_type = r.get("signal_type", "unknown")
        conf = r.get("confidence", 0.0) or 0.0
        by_day.setdefault(day, {}).setdefault(sig_type, []).append(conf)
    return by_day


def _build_time_series(records: List[Dict]) -> Dict:
    """构建 ECharts 时序数据"""
    by_day = _aggregate_by_day(records)
    days = sorted(by_day.keys())
    signal_types = sorted({r["signal_type"] for r in records})

    series = []
    for sig_type in signal_types:
        data = []
        for day in days:
            values = by_day[day].get(sig_type, [])
            data.append(round(sum(values) / len(values), 3) if values else 0)
        series.append({"name": sig_type, "type": "line", "smooth": True, "data": data})

    return {"days": days, "series": series}


def _build_pie_data(records: List[Dict]) -> List[Dict]:
    """构建 signal_type 分布饼图数据"""
    counts: Dict[str, int] = {}
    for r in records:
        sig_type = r.get("signal_type", "unknown")
        counts[sig_type] = counts.get(sig_type, 0) + 1
    return [{"name": k, "value": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


def _build_gauge_data(status: Dict) -> List[Dict]:
    """构建仪表盘数据"""
    buffer = status.get("retrain_buffer_size", 0)
    threshold = status.get("retrain_threshold", 40)
    pct = min(100, int(buffer / threshold * 100)) if threshold > 0 else 0
    return [
        {
            "name": "重训练缓冲",
            "value": pct,
            "detail": f"{buffer}/{threshold}",
        },
        {
            "name": "模型版本",
            "value": status.get("model_version", 0),
            "detail": "v" + str(status.get("model_version", 0)),
        },
        {
            "name": "磁盘版本数",
            "value": len(status.get("versions_on_disk", [])),
            "detail": str(len(status.get("versions_on_disk", []))) + " 个",
        },
    ]


def _build_version_rows(status: Dict) -> str:
    """构建版本历史表格行"""
    versions = status.get("versions_on_disk", [])
    if not versions:
        return '<tr><td colspan="4" style="text-align:center;color:#888;">'
    rows = []
    for v in versions:
        rows.append(
            f"<tr><td>v{v.get('version', '?')}</td>"
            f"<td>{v.get('mode', '?')}</td>"
            f"<td>{', '.join(v.get('dimensions', []))}</td>"
            f"<td>{v.get('timestamp', '?')}</td></tr>"
        )
    return "\n".join(rows)


def _build_recent_rows(records: List[Dict]) -> str:
    """构建最近记录表格行"""
    if not records:
        return '<tr><td colspan="5" style="text-align:center;color:#888;">'
    rows = []
    for r in records[:20]:
        rows.append(
            f"<tr><td>{r.get('created_at', '?')[:19]}</td>"
            f"<td>{r.get('signal_type', '?')}</td>"
            f"<td>{r.get('signal_value', '?')}</td>"
            f"<td>{r.get('confidence', 0):.2f}</td>"
            f"<td>{r.get('session_id', '?')[:20]}...</td></tr>"
        )
    return "\n".join(rows)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>Mnemos 漂移检测报告</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 20px;
            background: #f5f5f5;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 12px;
            margin-bottom: 20px;
        }
        .header h1 { margin: 0; font-size: 28px; }
        .header .meta { opacity: 0.9; margin-top: 10px; font-size: 14px; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }
        .card h2 {
            margin-top: 0;
            font-size: 18px;
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; }
        .chart { height: 300px; }
        .gauge { height: 220px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f8f9fa; font-weight: 600; color: #555; }
        .badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .badge-cold { background: #e3f2fd; color: #1976d2; }
        .badge-warm { background: #fff3e0; color: #f57c00; }
        .badge-hot { background: #e8f5e9; color: #388e3c; }
        .stat-box { text-align: center; padding: 20px; }
        .stat-box .number { font-size: 36px; font-weight: 700; color: #667eea; }
        .stat-box .label { color: #888; font-size: 14px; margin-top: 8px; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🧭 Mnemos 漂移检测报告</h1>
        <div class="meta">
            生成时间: __NOW__ |
            当前模式: <span class="badge badge-__MODE__">__MODE_UPPER__</span> |
            数据范围: 最近 30 天 |
            记录总数: __RECORD_COUNT__
        </div>
    </div>

    <div class="grid-3">
        <div class="card">
            <div class="stat-box">
                <div class="number">__RECORD_COUNT__</div>
                <div class="label">反馈记录</div>
            </div>
        </div>
        <div class="card">
            <div class="stat-box">
                <div class="number">__MODEL_VERSION__</div>
                <div class="label">模型版本</div>
            </div>
        </div>
        <div class="card">
            <div class="stat-box">
                <div class="number">__BUFFER_SIZE__</div>
                <div class="label">缓冲进度 / __BUFFER_THRESHOLD__</div>
            </div>
        </div>
    </div>

    <div class="grid-3">
        <div class="card">
            <h2>🎯 重训练缓冲</h2>
            <div id="gauge1" class="gauge"></div>
        </div>
        <div class="card">
            <h2>📊 模型版本</h2>
            <div id="gauge2" class="gauge"></div>
        </div>
        <div class="card">
            <h2>💾 磁盘版本</h2>
            <div id="gauge3" class="gauge"></div>
        </div>
    </div>

    <div class="card">
        <h2>📈 反馈信号时序趋势</h2>
        <div id="timeSeries" class="chart" style="height:400px;"></div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>🍰 信号类型分布</h2>
            <div id="pieChart" class="chart"></div>
        </div>
        <div class="card">
            <h2>📋 评分器状态</h2>
            <table>
                <tr><th>属性</th><th>值</th></tr>
                <tr><td>Domain</td><td>__DOMAIN__</td></tr>
                <tr><td>Mode</td><td>__MODE__</td></tr>
                <tr><td>Dimensions</td><td>__DIMENSIONS__</td></tr>
                <tr><td>Retrain Buffer</td><td>__BUFFER_SIZE__ / __BUFFER_THRESHOLD__</td></tr>
                <tr><td>Min Samples/Dim</td><td>__MIN_SAMPLES__</td></tr>
                <tr><td>Model Dir</td><td>__MODEL_DIR__</td></tr>
            </table>
        </div>
    </div>

    <div class="card">
        <h2>📦 模型版本历史</h2>
        <table>
            <tr><th>版本</th><th>模式</th><th>维度</th><th>时间</th></tr>
            __VERSION_ROWS__
        </table>
    </div>

    <div class="card">
        <h2>📝 最近 20 条反馈记录</h2>
        <table>
            <tr><th>时间</th><th>信号类型</th><th>标签</th><th>置信度</th><th>Session</th></tr>
            __RECENT_ROWS__
        </table>
    </div>
</div>

<script>
// 仪表盘
__GAUGE_DATA_JSON__.forEach((g, i) => {{
    const chart = echarts.init(document.getElementById('gauge' + (i+1)));
    chart.setOption({{
        series: [{{
            type: 'gauge',
            startAngle: 180,
            endAngle: 0,
            min: 0,
            max: i === 0 ? 100 : (i === 1 ? 10 : 10),
            splitNumber: 5,
            itemStyle: {{ color: '#667eea' }},
            progress: {{ show: true, width: 18 }},
            pointer: {{ show: false }},
            axisLine: {{ lineStyle: {{ width: 18 }} }},
            axisTick: {{ show: false }},
            splitLine: {{ length: 10, lineStyle: {{ width: 2, color: '#999' }} }},
            axisLabel: {{ distance: 15, color: '#999', fontSize: 10 }},
            detail: {{
                valueAnimation: true,
                fontSize: 24,
                offsetCenter: [0, '30%'],
                formatter: function(v) {{ return g.detail; }}
            }},
            data: [{{ value: g.value, name: g.name }}]
        }}]
    }});
}});

// 时序图
const tsChart = echarts.init(document.getElementById('timeSeries'));
tsChart.setOption({{
    tooltip: {{ trigger: 'axis' }},
    legend: {{ data: __TS_LEGEND__ }},
    grid: {{ left: '3%', right: '4%', bottom: '3%', containLabel: true }},
    xAxis: {{ type: 'category', boundaryGap: false, data: __TS_DAYS__ }},
    yAxis: {{ type: 'value', min: 0, max: 1 }},
    series: __TS_SERIES__
}});

// 饼图
const pieChart = echarts.init(document.getElementById('pieChart'));
pieChart.setOption({{
    tooltip: {{ trigger: 'item' }},
    series: [{{
        type: 'pie',
        radius: ['40%', '70%'],
        avoidLabelOverlap: false,
        itemStyle: {{ borderRadius: 8, borderColor: '#fff', borderWidth: 2 }},
        label: {{ show: false }},
        data: __PIE_DATA__
    }}]
}});

window.addEventListener('resize', () => {{
    tsChart.resize();
    pieChart.resize();
}});
</script>
</body>
</html>"""


def generate_report(output_path: Optional[Path] = None) -> Path:
    """生成漂移检测报告 HTML"""
    if output_path is None:
        output_path = Path.home() / ".mnemos" / "reports" / "drift_report.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = _fetch_ground_truth(days=30)
    status = _fetch_scorer_status()
    time_series = _build_time_series(records)
    pie_data = _build_pie_data(records)
    gauge_data = _build_gauge_data(status)

    # 处理空状态默认值
    mode = status.get("mode", "cold")
    version_rows_html = _build_version_rows(status)
    recent_rows_html = _build_recent_rows(records)

    # 如果表格为空，补全闭合标签
    if version_rows_html.endswith('>') and not version_rows_html.endswith('</tr>'):
        version_rows_html += '暂无版本记录</td></tr>'
    if recent_rows_html.endswith('>') and not recent_rows_html.endswith('</tr>'):
        recent_rows_html += '暂无记录</td></tr>'

    html = (
        HTML_TEMPLATE
        .replace("__NOW__", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        .replace("__MODE__", mode)
        .replace("__MODE_UPPER__", mode.upper())
        .replace("__RECORD_COUNT__", str(len(records)))
        .replace("__MODEL_VERSION__", str(status.get("model_version", 0)))
        .replace("__BUFFER_SIZE__", str(status.get("retrain_buffer_size", 0)))
        .replace("__BUFFER_THRESHOLD__", str(status.get("retrain_threshold", 40)))
        .replace("__DOMAIN__", status.get("domain", "?"))
        .replace("__DIMENSIONS__", ", ".join(status.get("dimensions", [])))
        .replace("__MIN_SAMPLES__", str(status.get("min_samples_per_dim", 12)))
        .replace("__MODEL_DIR__", status.get("model_dir", "?"))
        .replace("__VERSION_ROWS__", version_rows_html)
        .replace("__RECENT_ROWS__", recent_rows_html)
        .replace("__GAUGE_DATA_JSON__", json.dumps(gauge_data))
        .replace("__TS_LEGEND__", json.dumps([s["name"] for s in time_series["series"]]))
        .replace("__TS_DAYS__", json.dumps(time_series["days"]))
        .replace("__TS_SERIES__", json.dumps(time_series["series"]))
        .replace("__PIE_DATA__", json.dumps(pie_data))
    )

    output_path.write_text(html, encoding="utf-8")
    return output_path


def main():
    path = generate_report()
    print(f"漂移检测报告已生成: {path}")
    import os
    os.system(f"open '{path}'")


if __name__ == "__main__":
    main()
