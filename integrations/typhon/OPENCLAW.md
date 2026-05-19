# Mnemos - OpenClaw 集成指南

## 系统简介

Mnemos 是一个全自动 AI 知识管理系统，负责将用户的 AI 对话、文件、笔记等原始素材自动提炼为结构化知识，并注入回 AI 的工作流中。

**核心设计原则：同源复用** — Mnemos 不直接调用任何 LLM API，所有需要 LLM 的任务都委托给宿主 Agent（你）处理。

## OpenClaw 的角色

在 Mnemos 生态中，OpenClaw 是**分析与推理专家**：
- 擅长深度分析和逻辑推理
- 负责将复杂对话蒸馏为结构化知识
- 参与知识图谱构建和关联分析

## 连接方式

### 1. Session Hooks

Mnemos 通过 SQLite 配置表监听 OpenClaw 的会话事件：

- `session_start`: OpenClaw 启动新会话时触发
- `session_end`: OpenClaw 结束会话时触发

配置数据库：`~/.openclaw/sessions.db`
配置表：`mnemos_config`, `mnemos_events`

### 2. 事件总线

OpenClaw 通过文件系统事件队列与 Mnemos Daemon 通信：
- 事件目录：`~/.mnemos/events/`
- 事件类型：`session.start`, `session.end`, `distill.request`

### 3. 蒸馏任务

当 Mnemos 有蒸馏任务时：
1. 任务写入 SQLite `mnemos_tasks` 表
2. 通知标记写入 `mnemos_events` 表
3. OpenClaw 读取任务，执行蒸馏，将结果写入指定输出路径

## 可用的 MCP 工具

当通过 MCP 协议连接时，OpenClaw 可以调用以下工具：

| 工具 | 用途 |
|------|------|
| `wiki_search` | 搜索 Wiki 知识库 |
| `wiki_read` | 读取 Wiki 页面 |
| `session_search` | 搜索历史会话记录 |
| `knowledge_distill` | 触发知识蒸馏 |
| `preflight_inject` | 任务前知识装载 |
| `guard_check` | 执行中守护检查 |
| `persona_summary` | 获取用户画像 |
| `health_check` | 系统健康检查 |

## 调试命令

```bash
# 检查 Mnemos 状态
python -m mnemos_cli status

# 查看蒸馏队列
python -m mnemos_cli distill --check

# 手动触发画像更新
python -m mnemos_cli persona update

# 系统诊断
python -m mnemos_cli doctor
```

## 故障排查

### 蒸馏任务不处理
1. 检查 daemon 是否运行：`python -m mnemos_daemon status`
2. 检查 SQLite 数据库中 `mnemos_events` 表是否有新事件
3. 检查 `mnemos_tasks` 表中 `status='pending'` 的任务

### 知识未加载
1. 确认 hooks 已安装：`python -m mnemos_cli agent install`
2. 检查 SQLite 配置是否正确写入
