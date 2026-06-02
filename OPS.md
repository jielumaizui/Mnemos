# Mnemos 运维手册

> 版本: v2.0.0 | 最后更新: 2026-06-01

## 1. 快速诊断

### 1.1 一键健康检查

```bash
cd ~/mnemos
python3 -m core.ops.health_check
# JSON 输出（用于脚本集成）
python3 -m core.ops.health_check --json
```

检查项：
- 进程状态（daemon、Memos）
- Memos API 可达性
- Amphora 蒸馏队列深度
- EventBus 积压 / 死信队列
- 磁盘空间 / 数据库大小
- LLM API 可用性

### 1.2 启动前置检查

daemon 启动时自动运行 `_run_preflight_checks()`，检查：
- 关键目录可写性（data、wiki、distill_queue、distill_output）
- Memos API 连接
- Agent 可用性（claude/kimi/codex 等）
- 文件句柄余量
- 数据库大小（events.db > 500MB 告警）
- 信号数据库连通性

## 2. 核心服务管理

### 2.1 daemon 启停

```bash
# 启动（前台，带日志）
python3 mnemos_daemon.py

# 后台启动（推荐）
nohup python3 mnemos_daemon.py > daemon.log 2>&1 &

# 停止（发送 SIGTERM）
pkill -f mnemos_daemon.py

# 检查是否运行
pgrep -f mnemos_daemon.py
```

daemon 启动时会写入 PID 文件到 `~/.mnemos/daemon.pid`，防止重复启动。

### 2.2 服务模块

daemon 包含 4 个核心服务：

| 服务 | 间隔 | 功能 |
|------|------|------|
| L1 同步 | 实时 | 监控 Agent session 文件变化 → 自动同步到 Memos |
| 收件箱扫描 | 10min | 扫描 `data/inbox` 目录，处理文件进 Memos |
| 心跳 | 60s | 健康评分 + 争议扫描 + 新鲜度检查 + 评分器训练 + 搜索健康检查 |
| 蒸馏 Worker | 事件驱动 | 消费 amphora 队列，执行七层蒸馏流水线 |

### 2.3 各模块健康指标

**心跳中的关键调度：**
- 每 5 次心跳（5min）：报告蒸馏评分器状态（模式/缓冲/版本）
- 每 30 次心跳（30min）：搜索索引健康检查 + 问答检索缓存刷新
- 每 720 次心跳（12h）：synthetic ground_truth 注入 + 评分器训练调度
- 每 1440 次心跳（24h）：争议扫描 + 知识新鲜度检查

## 3. 队列与任务管理

### 3.1 Amphora 蒸馏队列

```bash
# 查看队列统计
python3 -m core.kia.amphora --stats

# 列出待处理任务
python3 -m core.kia.amphora --list

# 手动标记任务完成
python3 -m core.kia.amphora --done <session_id> --output <wiki_path>

# 清理 7 天前的完成/失败任务
python3 -m core.kia.amphora --cleanup
```

### 3.2 EventBus 事件队列

数据库：`~/.mnemos/events.db`

```bash
# 查看 pending 事件数量
sqlite3 ~/.mnemos/events.db "SELECT COUNT(*) FROM events WHERE status='pending'"

# 查看死信队列
sqlite3 ~/.mnemos/events.db "SELECT COUNT(*) FROM dead_letters"

# 清理旧事件（保留最近 30 天）
sqlite3 ~/.mnemos/events.db "DELETE FROM events WHERE created_at < datetime('now', '-30 days') AND status IN ('done', 'archived')"
```

告警阈值：
- pending > 1000：队列深度告警
- dead_letters > 10：死信队列告警

## 4. 常见问题排查

### 4.1 daemon 无法启动

```bash
# 检查 PID 文件是否残留
rm ~/.mnemos/daemon.pid

# 检查端口冲突（如果有 web 服务）
lsof -i :8080

# 检查目录权限
ls -la ~/.mnemos/ ~/Documents/Obsidian\ Vault/wiki/
```

### 4.2 蒸馏不产出 wiki

排查路径：
1. `python3 -m core.kia.amphora --stats` — 确认队列有 pending 任务
2. 检查 `mnemos_daemon.py` 日志中的 hephaestus worker 输出
3. 检查 `~/.mnemos/wiki_state.db` 中的 `processed_sessions` — 是否已被标记为 processed
4. 检查 LLM API 可用性：`python3 -m core.ops.health_check`

### 4.3 Memos 同步失败

```bash
# 测试 Memos API
curl -H "Authorization: Bearer <token>" \
  "<memos_url>/api/v1/memos?limit=1"

# 检查配置
python3 -c "from core.config import get_config; c=get_config(); print(c.memos_api_url, c.memos_token[:8]+'...')"
```

### 4.4 评分器一直处于 COLD 模式

- WARM 阈值：总样本 ≥ 30（已修复，原逻辑需已有模型）
- synthetic ground_truth 每 12h 从 session_signals 自动注入
- 手动加速：`python3 -c "from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2; AdaptiveScorerV2._bootstrap_if_needed()"`

### 4.5 Wiki 页面堆积在 Inbox

- Inbox 归档是手动/半自动过程（目前 126 个页面已归档到各分类目录）
- 自动归档：运行 `python3 scripts/auto_archive_inbox.py`（如有）
- 手动归档：按 frontmatter `类型` 字段移动到对应目录

## 5. 数据库维护

### 5.1 主要数据库

| 数据库 | 路径 | 用途 |
|--------|------|------|
| events.db | ~/.mnemos/events.db | EventBus 事件队列 |
| wiki_state.db | ~/.mnemos/wiki_state.db | 已处理 session、wiki 页面索引 |
| user_signals.db | ~/.mnemos/user_signals.db | 用户行为信号、画像数据 |
| distill_queue.db | ~/.claude/distill_queue.db | Amphora 蒸馏队列 |

### 5.2 定期维护

```bash
# 清理 events.db（保留 30 天）
sqlite3 ~/.mnemos/events.db "VACUUM"

# 清理 wiki_state.db 中的旧 processed_sessions
sqlite3 ~/.mnemos/wiki_state.db \
  "DELETE FROM processed_sessions WHERE processed_at < datetime('now', '-90 days') AND distill_method LIKE 'skipped%'"

# 检查数据库大小
du -h ~/.mnemos/*.db ~/.claude/distill_queue.db
```

## 6. 监控与告警

### 6.1 内置监控

daemon 心跳自动监控：
- 系统健康度（OpsScorer）
- 蒸馏评分器状态（模式/版本/缓冲）
- 争议扫描结果
- 知识新鲜度告警（> 90 天未更新）
- 搜索索引健康

### 6.2 关键日志

```bash
# daemon 实时日志
tail -f daemon.log

# 只看错误
grep -E "ERROR|FAIL|异常" daemon.log

# 只看蒸馏相关
grep -E "蒸馏|distill|hephaestus" daemon.log
```

### 6.3 外部集成

如需接入 Prometheus/Grafana，可扩展 `core/ops/health_check.py` 的 `--json` 输出，配合 node_exporter textfile collector 或 pushgateway。

## 7. 备份与恢复

### 7.1 关键数据备份

```bash
# 备份 wiki 目录
rsync -av ~/Documents/Obsidian\ Vault/wiki/ ~/Backups/mnemos-wiki-$(date +%Y%m%d)/

# 备份数据库
mkdir -p ~/Backups/mnemos-db-$(date +%Y%m%d)
cp ~/.mnemos/*.db ~/Backups/mnemos-db-$(date +%Y%m%d)/
cp ~/.claude/distill_queue.db ~/Backups/mnemos-db-$(date +%Y%m%d)/
```

### 7.2 恢复

直接还原备份目录即可。数据库是 SQLite，无需特殊恢复流程。

## 8. 联系与升级

- 代码仓库：`~/mnemos`（个人版）/ `~/Projects/memos-wiki-pkm`（通用版）
- 健康检查脚本：`python3 -m core.ops.health_check`
- 蓝图文档：`docs/blueprint.md`（如有）
