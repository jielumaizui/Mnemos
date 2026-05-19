# Mnemos - Hermes 集成指南

## 系统简介

Mnemos 是一个全自动 AI 知识管理系统，负责将用户的 AI 对话、文件、笔记等原始素材自动提炼为结构化知识，并注入回 AI 的工作流中。

**核心设计原则：同源复用** — Mnemos 不直接调用任何 LLM API，所有需要 LLM 的任务都委托给宿主 Agent（你）处理。

## Hermes 的角色

在 Mnemos 生态中，Hermes 是**信息检索专家**：
- 擅长快速信息检索和多源搜索
- 负责从 Memos/Wiki 中提取相关知识
- 参与蒸馏任务，将对话提炼为结构化知识

## 连接方式

### 1. Session Hooks

Mnemos 通过 hooks 监听 Hermes 的会话事件：

- `session_start`: Hermes 启动新会话时触发 → Mnemos 预加载相关知识
- `session_end`: Hermes 结束会话时触发 → Mnemos 将对话入蒸馏队列

配置文件：`~/.hermes/config.toml`

### 2. 事件总线

Hermes 通过文件系统事件队列与 Mnemos Daemon 通信：
- 事件目录：`~/.mnemos/events/`
- 事件类型：`session.start`, `session.end`, `distill.request`

### 3. 蒸馏任务

当 Mnemos 有蒸馏任务时：
1. 任务写入 `~/.hermes/inbox/mnemos_distill_{session_id}.json`
2. 通知标记写入 `~/.hermes/inbox/.mnemos_notify`
3. Hermes 读取任务，执行蒸馏，将结果写入指定输出路径

## 可用的 MCP 工具

当通过 MCP 协议连接时，Hermes 可以调用以下工具：

| 工具 | 用途 |
|------|------|
| `wiki_search` | 搜索 Wiki 知识库 |
| `session_search` | 搜索历史会话记录 |
| `knowledge_ingest` | 将知识写入 Memos |
| `preflight_inject` | 任务前知识装载 |
| `guard_check` | 执行中守护检查 |
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
2. 检查 `~/.hermes/inbox/` 是否有 `.mnemos_notify` 文件
3. 检查任务 JSON 文件的 `output_path` 是否正确

### 知识未加载
1. 确认 hooks 已安装：`python -m mnemos_cli agent install`
2. 检查事件目录权限：`~/.mnemos/events/`
