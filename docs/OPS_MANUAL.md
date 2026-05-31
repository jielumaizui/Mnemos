# Mnemos 运维手册

> 版本: Phase 8  |  最后更新: 2026-06-01

---

## 一、日常检查清单（每日/每周）

### 每日检查
```bash
# 运行健康检查
python3 -m core.ops.health_check

# 或输出 JSON 供脚本处理
python3 -m core.ops.health_check --json
```

关注指标：
| 指标 | 健康阈值 | 异常时动作 |
|------|----------|-----------|
| amphora pending | < 50 | >50 时检查 worker 是否运行 |
| amphora failed | < 10 | >10 时检查 API 配额/网络 |
| EventBus pending | < 1000 | >1000 时重启 daemon 或清理旧事件 |
| 磁盘使用 | < 90% | >90% 时清理 distill_output/归档旧 wiki |
| Inbox 堆积 | < 200 | >200 时手动归档到主题目录 |

### 每周检查
- 运行 `mnemos doctor` 全面诊断
- 检查 `~/.mnemos/alerts/` 目录是否有新告警
- 检查 API 调用日志，确认无异常消耗

---

## 二、核心组件状态检查

### 1. Daemon 状态
```bash
# 检查进程
pgrep -f mnemos_daemon.py

# 查看日志
tail -f ~/.mnemos/logs/daemon.log

# 重启 daemon
python3 mnemos_daemon.py restart
```

### 2. Memos 状态
```bash
# Memos 默认端口 5230
curl -s http://localhost:5230/api/v1/memos?limit=1

# 如果 Memos 未启动，手动启动
# (根据你的 Memos 安装方式)
```

### 3. Amphora 队列
```bash
python3 -c "
from core.kia import amphora
print('Pending:', len(amphora.list_pending()))
print('Processing:', len(amphora.list_processing()))
print('Done:', amphora.get_task_count('done'))
print('Failed:', amphora.get_task_count('failed'))
"
```

### 4. EventBus 积压清理
```bash
# 查看 pending 事件数量
python3 -c "
import sqlite3
conn = sqlite3.connect('~/.mnemos/events.db')
c = conn.cursor()
c.execute(\"SELECT COUNT(*) FROM events WHERE status='pending'\")
print('Pending:', c.fetchone()[0])
conn.close()
"

# 清理旧事件（保留最近 7 天）
python3 -c "
import sqlite3
from datetime import datetime, timedelta
conn = sqlite3.connect('~/.mnemos/events.db')
c = conn.cursor()
cutoff = (datetime.now() - timedelta(days=7)).isoformat()
c.execute('DELETE FROM events WHERE created_at < ? AND status=\"pending\"', (cutoff,))
conn.commit()
print('Deleted:', c.rowcount)
conn.close()
"
```

---

## 三、常见问题排查

### Q1: 蒸馏任务永远卡在 processing
**现象**: amphora 中 processing 数量不减少
**根因**: 宿主 Agent（Claude）只写占位符，从不真正执行蒸馏
**修复**: 
- 已在 Phase 7 修复：`process_one_task` 检测占位符后自动切换同步 API 蒸馏
- 如果仍有旧任务卡住，运行 `amphora.reset_timeouts(timeout_minutes=1)` 恢复为 pending

### Q2: "输出格式无效：无法从输出中提取有效 JSON"
**现象**: 蒸馏任务失败，retry 耗尽
**根因**: LLM API（DeepSeek-V3）偶尔不返回严格 JSON
**修复**:
- 检查 `OPENAI_BASE_URL` 和模型配置
- 降低 `distill.max_tasks_per_cycle` 避免并发过高
- 手动重试：`python3 -c "from core.kia import amphora; amphora.reset_timeouts(1)"`

### Q3: EventBus 队列深度超过 1000
**现象**: 日志中出现 `[EventBus] 队列深度 XXXX 超过告警阈值 1000`
**根因**: 事件生产速度 > 消费速度，或 daemon 未运行
**修复**:
- 确认 daemon 在运行：`pgrep -f mnemos_daemon.py`
- 清理旧 pending 事件（见上文）
- 增加消费端处理能力

### Q4: Memos API 返回 401/403
**现象**: sync 失败，MemosClient 报错
**根因**: Token 过期或权限变更
**修复**:
- 在 Memos 网页端生成新 token
- 更新 `~/.mnemos/config.toml` 中的 `memos_token`
- 重启 daemon

### Q5: "table ground_truth_signals has no column named latency_hours"
**现象**: ScorerV2 报错
**根因**: 数据库 schema 版本不匹配
**修复**: 删除旧数据库让其重建，或运行 schema 迁移
```bash
rm ~/.mnemos/user_signals.db
# 重启 daemon 后会自动重建
```

### Q6: 磁盘空间不足
**现象**: 健康检查显示磁盘使用 >90%
**修复**:
```bash
# 1. 清理 distill_output 占位符
rm ~/.mnemos/distill_output/*.md

# 2. 归档旧 Inbox 文件到主题目录
python3 mnemos_cli.py inbox archive --older-than 30

# 3. 清理事件数据库旧记录
# (见 EventBus 清理命令)
```

---

## 四、备份和恢复

### 备份清单
```bash
# 关键数据目录
BACKUP_DIRS=(
    ~/.mnemos/              # 主数据（排除 logs/）
    ~/.claude/distill_queue.db
    ~/.claude/distill_messages/
    ~/Documents/Obsidian\ Vault/wiki/  # Wiki 内容
)

# 快速备份（不含大文件）
tar czf mnemos_backup_$(date +%Y%m%d).tar.gz \
  --exclude='*/logs/*' \
  --exclude='*/.cache/*' \
  ~/.mnemos/*.db \
  ~/.claude/*.db \
  ~/.claude/distill_messages/ \
  ~/Documents/Obsidian\ Vault/wiki/
```

### 恢复
```bash
# 停止 daemon
pkill -f mnemos_daemon.py

# 恢复数据
tar xzf mnemos_backup_20260601.tar.gz -C /

# 重启 daemon
python3 mnemos_daemon.py start
```

---

## 五、性能调优

### API 限流
- SiliconFlow RPM: 2000, TPM: 500000
- 如果频繁触发限流，调整 `distill.max_tasks_per_cycle`（默认 5）

### EventBus 配置
```toml
# ~/.mnemos/config.toml
[event_bus]
queue_depth_alert = 1000    # 队列深度告警阈值
max_queue_depth = 10000     # 队列上限
max_recover_events = 1000   # 每次恢复事件数
```

### 数据库性能
- SQLite WAL 模式已启用，无需额外配置
- 如果数据库 >100MB，考虑 VACUUM

---

## 六、紧急联系人/资源

| 组件 | 检查命令 | 日志位置 |
|------|----------|----------|
| Daemon | `pgrep -f mnemos_daemon.py` | `~/.mnemos/logs/daemon.log` |
| Memos | `curl localhost:5230/api/v1/memos` | Memos 自带日志 |
| Amphora | `python3 -m core.ops.health_check` | 无独立日志 |
| EventBus | 检查 `~/.mnemos/events.db` | 无独立日志 |
| LLM API | `python3 -c "from core.hephaestus.distillation_engine import HostAgentCaller; HostAgentCaller(force_provider='api')._invoke('hi')"` | 无独立日志 |
