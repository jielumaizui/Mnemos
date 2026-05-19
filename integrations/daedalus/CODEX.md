# Mnemos - Codex 集成指南

## 系统简介

Mnemos 是一个全自动 AI 知识管理系统，负责将用户的 AI 对话、文件、笔记等原始素材自动提炼为结构化知识，并注入回 AI 的工作流中。

**核心设计原则：同源复用** — Mnemos 不直接调用任何 LLM API，所有需要 LLM 的任务都委托给宿主 Agent（你）处理。

## Codex 的角色

在 Mnemos 生态中，Codex 是**代码生成专家**：
- 专注代码生成任务
- 负责将代码相关对话蒸馏为结构化知识
- 通过 wrapper 脚本与 Mnemos 集成

## 连接方式

### 1. Wrapper 脚本

Codex 没有官方 hook 机制，采用 Shell Wrapper 方案：

- `mnemos-codex` 命令包装 `codex`，在前后注入 Mnemos 逻辑
- 脚本位置：`~/.codex/mnemos_wrapper.py`
- Windows wrapper：`~/.codex/mnemos-codex.bat`
- Unix wrapper：`~/.codex/mnemos-codex`

使用方法：
```bash
# 代替 codex 命令
mnemos-codex --session-start --working-dir . --user-message "your prompt"
```

### 2. 事件总线

Codex 通过文件系统事件队列与 Mnemos Daemon 通信：
- 事件目录：`~/.mnemos/events/`
- 事件类型：`session.start`, `session.end`, `distill.request`

### 3. 蒸馏任务

当 Mnemos 有蒸馏任务时：
1. 任务写入 `~/.codex/mnemos_distill_tasks/{session_id}.json`
2. 通知标记写入 `~/.codex/mnemos_distill_tasks/.mnemos_notify`
3. Codex 读取任务，执行蒸馏，将结果写入指定输出路径

## 可用的 MCP 工具

Codex 目前不直接支持 MCP，但可以通过 wrapper 脚本调用 Mnemos CLI：

```bash
# 搜索知识库
python -m mnemos_cli wiki search "your query"

# 保存会话
python -m mnemos_cli session save --session-id abc123 --messages [...]

# 触发蒸馏
python -m mnemos_cli distill --session-id abc123

# 系统诊断
python -m mnemos_cli doctor
```

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

# 检查 Codex wrapper 是否安装
ls ~/.codex/mnemos_wrapper.py ~/.codex/mnemos-codex
```

## 故障排查

### Wrapper 未生效
1. 确认 `~/.codex/` 目录下有 wrapper 脚本
2. Unix: `chmod +x ~/.codex/mnemos-codex`
3. Windows: 确保 `.bat` 文件在 PATH 中

### 蒸馏任务不处理
1. 检查 `~/.codex/mnemos_distill_tasks/` 目录
2. 检查 `.mnemos_notify` 文件内容
3. 运行 `python -m mnemos_daemon status`

### PATH 问题
Codex wrapper 需要加入 PATH：
```bash
# 临时添加（当前会话）
export PATH="$HOME/.codex:$PATH"

# 永久添加（添加到 ~/.bashrc 或 ~/.zshrc）
echo 'export PATH="$HOME/.codex:$PATH"' >> ~/.zshrc
```
