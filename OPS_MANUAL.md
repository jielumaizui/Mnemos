# Mnemos 运维手册

> 版本: v2.0.0-beta | 最后更新: 2026-06-02

---

## 一、快速诊断

### 1.1 一键健康检查

```bash
cd ~/mnemos
python3 -m core.ops.health_check
# JSON 输出（用于脚本集成）
python3 -m core.ops.health_check --json
```

检查项：进程状态、Memos API、Amphora 队列、EventBus 积压、磁盘空间、LLM API。

### 1.2 CLI 诊断

```bash
# 全面诊断（含来源分布、截断数据、KG 统计等）
python3 mnemos_cli.py doctor

# 检查历史截断标记
python3 scripts/mark_truncated.py --count
```

---

## 二、日常检查清单

### 每日
- `python3 -m core.ops.health_check`
- 关注 amphora pending（<50）、EventBus pending（<1000）、磁盘（<90%）

### 每周
- `python3 mnemos_cli.py doctor`
- 检查 `~/.mnemos/alerts/` 是否有新告警
- 查看 daemon 日志中的 ERROR/FAIL

---

## 三、核心服务管理

### Daemon 启停

```bash
# 前台启动
python3 mnemos_daemon.py

# 后台启动
nohup python3 mnemos_daemon.py > daemon.log 2>&1 &

# 停止
pkill -f mnemos_daemon.py

# 检查
pgrep -f mnemos_daemon.py
```

daemon 启动时自动写入 PID 文件到 `~/.mnemos/daemon.pid`。

### 服务模块

| 服务 | 间隔 | 功能 |
|------|------|------|
| L1 同步 | 实时 | Agent session 文件变化 → 自动同步到 Memos |
| 收件箱扫描 | 10min | 扫描 `data/inbox`，处理文件进 Memos |
| 心跳 | 60s | 健康评分 + 争议扫描 + 新鲜度 + 评分器训练 + 搜索健康 |
| 蒸馏 Worker | 事件驱动 | 消费 amphora 队列，执行七层蒸馏流水线 |

### 心跳关键调度
- 每 5 次（5min）：蒸馏评分器状态报告
- 每 30 次（30min）：搜索索引健康检查 + 缓存刷新
- 每 720 次（12h）：synthetic ground_truth 注入 + 评分器训练调度
- 每 1440 次（24h）：争议扫描 + 知识新鲜度检查

---

## 四、队列与任务管理

### Amphora 蒸馏队列

```bash
python3 -m core.kia.amphora --stats
python3 -m core.kia.amphora --list
python3 -m core.kia.amphora --cleanup      # 清理 7 天前的完成/失败任务
```

### EventBus 事件队列

数据库：`~/.mnemos/events.db`

```bash
# 查看 pending
sqlite3 ~/.mnemos/events.db "SELECT COUNT(*) FROM events WHERE status='pending'"

# 查看死信
sqlite3 ~/.mnemos/events.db "SELECT COUNT(*) FROM dead_letters"

# 清理旧事件（保留 30 天）
sqlite3 ~/.mnemos/events.db "DELETE FROM events WHERE created_at < datetime('now', '-30 days') AND status IN ('done', 'archived')"
```

告警阈值：pending > 1000，dead_letters > 10。

---

## 五、常见问题排查

### Q1: daemon 无法启动
```bash
rm ~/.mnemos/daemon.pid        # 清理残留 PID
lsof -i :8080                  # 检查端口冲突
ls -la ~/.mnemos/              # 检查目录权限
```

### Q2: 蒸馏不产出 wiki
1. `python3 -m core.kia.amphora --stats` — 确认有 pending 任务
2. 查看 daemon 日志中的 hephaestus worker 输出
3. 检查 `~/.mnemos/wiki_state.db` 的 `processed_sessions` — 是否已被标记
4. `python3 -m core.ops.health_check` — 检查 LLM API

### Q3: Memos 同步失败
```bash
# 测试 API
curl -H "Authorization: Bearer <token>" "<memos_url>/api/v1/memos?limit=1"

# 检查配置
python3 -c "from core.config import get_config; c=get_config(); print(c.memos_api_url)"
```

### Q4: 评分器一直处于 COLD 模式
- WARM 阈值：总样本 ≥ 30（已修复）
- synthetic ground_truth 每 12h 自动注入
- 手动加速：
  ```bash
  python3 -c "from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2; AdaptiveScorerV2._bootstrap_if_needed()"
  ```

### Q5: EventBus 队列深度超过 1000
- 确认 daemon 在运行：`pgrep -f mnemos_daemon.py`
- 清理旧 pending 事件（见上文）
- 如持续积压，检查事件消费端日志

### Q6: Wiki 页面堆积在 Inbox
- 按 frontmatter `类型` 字段手动归档到对应目录
- Inbox 归档目前为半自动过程

### Q7: 历史截断数据
```bash
# 扫描并记录截断标记
python3 scripts/mark_truncated.py

# 查看截断数量
python3 scripts/mark_truncated.py --count
```
新逻辑已移除截断（`save_long_content` 自动分片），此脚本仅用于标记历史数据。

---

## 六、数据库维护

### 主要数据库

| 数据库 | 路径 | 用途 |
|--------|------|------|
| events.db | ~/.mnemos/events.db | EventBus 事件队列 |
| wiki_state.db | ~/.mnemos/wiki_state.db | 已处理 session、wiki 页面索引 |
| user_signals.db | ~/.mnemos/user_signals.db | 用户行为信号、画像数据 |
| distill_queue.db | ~/.claude/distill_queue.db | Amphora 蒸馏队列 |
| mnemos.db | ~/.mnemos/mnemos.db | 评分模型、训练队列、截断记录 |

### 定期维护

```bash
# 清理 events.db
sqlite3 ~/.mnemos/events.db "VACUUM"

# 清理旧 processed_sessions（跳过 90 天前的 skipped）
sqlite3 ~/.mnemos/wiki_state.db \
  "DELETE FROM processed_sessions WHERE processed_at < datetime('now', '-90 days') AND distill_method LIKE 'skipped%'"

# 检查数据库大小
du -h ~/.mnemos/*.db ~/.claude/distill_queue.db
```

---

## 七、备份与恢复

### 备份

```bash
# wiki 目录
rsync -av ~/Documents/Obsidian\ Vault/wiki/ ~/Backups/mnemos-wiki-$(date +%Y%m%d)/

# 数据库
mkdir -p ~/Backups/mnemos-db-$(date +%Y%m%d)
cp ~/.mnemos/*.db ~/Backups/mnemos-db-$(date +%Y%m%d)/
cp ~/.claude/distill_queue.db ~/Backups/mnemos-db-$(date +%Y%m%d)/
```

### 恢复

直接还原备份目录即可。SQLite 无需特殊恢复流程。

---

## 八、监控与日志

### 关键日志

```bash
tail -f daemon.log                              # 实时日志
grep -E "ERROR|FAIL|异常" daemon.log            # 只看错误
grep -E "蒸馏|distill|hephaestus" daemon.log    # 蒸馏相关
```

### 外部集成

如需 Prometheus/Grafana，可扩展 `core/ops/health_check.py --json` 输出，配合 node_exporter textfile collector 或 pushgateway。

---

## 九、性能调优

### API 限流
- SiliconFlow RPM: 2000, TPM: 500000
- 频繁限流时调低 `distill.max_tasks_per_cycle`（默认 5）

### EventBus 配置
```toml
# ~/.mnemos/config.toml
[event_bus]
queue_depth_alert = 1000
max_queue_depth = 10000
max_recover_events = 1000
```

### 数据库
- SQLite WAL 模式已启用
- 数据库 > 100MB 时考虑 `VACUUM`

---

## 十、资源速查

| 组件 | 检查命令 | 日志/路径 |
|------|----------|-----------|
| Daemon | `pgrep -f mnemos_daemon.py` | `~/.mnemos/logs/daemon.log` |
| Memos | `curl localhost:5230/api/v1/memos` | Memos 自带日志 |
| Amphora | `python3 -m core.kia.amphora --stats` | `~/.claude/distill_queue.db` |
| EventBus | `sqlite3 ~/.mnemos/events.db` | `~/.mnemos/events.db` |
| LLM API | `python3 -m core.ops.health_check` | daemon.log |
| 截断扫描 | `python3 scripts/mark_truncated.py --count` | `~/.mnemos/mnemos.db` |
