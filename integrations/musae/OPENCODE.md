# Mnemos - OpenCode 集成指南

## 系统简介

Mnemos 是一个全自动 AI 知识管理系统，负责将用户的 AI 对话、文件、笔记等原始素材自动提炼为结构化知识，并注入回 AI 的工作流中。

**核心设计原则：同源复用** — Mnemos 不直接调用任何 LLM API，所有需要 LLM 的任务都委托给宿主 Agent（你）处理。

## OpenCode 的角色

在 Mnemos 生态中，OpenCode 是**代码理解与生成专家**：
- 擅长代码理解和生成
- 负责将代码相关对话蒸馏为结构化知识
- 通过 MCP 协议直接调用 Mnemos 工具

## 连接方式

### 1. MCP 服务器

OpenCode 原生支持 MCP 协议。Mnemos 提供 MCP 服务器 `mnemos_cli.py mcp serve`。

当前 OpenCode 官方配置：`~/.config/opencode/opencode.json`
```json
{
  "mcp": {
    "mnemos": {
      "type": "local",
      "command": ["python3", "/path/to/mnemos_cli.py", "mcp", "serve"],
      "enabled": true,
      "timeout": 10000
    }
  },
  "instructions": ["/home/user/.mnemos/active_policy/MNEMOS_ACTIVE.md"]
}
```

Mnemos 同时保留旧版 `~/.opencode/settings.json` 写入，用于兼容旧 OpenCode 配置。

### 2. Session Hooks

兼容配置文件：`~/.opencode/settings.json` 中的 `hooks` 字段

- `session_start`: OpenCode 启动新会话时触发
- `session_end`: OpenCode 结束会话时触发

`mnemos agent install opencode` 会同时写入：
- 官方 MCP 配置
- Mnemos Active Policy
- legacy hooks/wrapper

### 3. 蒸馏任务

当 Mnemos 有蒸馏任务时：
1. 任务写入 `~/.opencode/mnemos_tasks/{session_id}.json`
2. 通知标记写入 `~/.opencode/mnemos_tasks/.mnemos_notify`
3. OpenCode 读取任务，执行蒸馏，将结果写入指定输出路径

## 可用的 MCP 工具

通过 MCP 协议，OpenCode 可以直接调用以下工具：

| 工具 | 用途 | 使用场景 |
|------|------|----------|
| `wiki_search` | 搜索 Wiki 知识库 | 需要查找历史知识时 |
| `wiki_read` | 读取 Wiki 页面 | 读取特定知识条目 |
| `wiki_write` | 写入 Wiki 页面 | 生成新知识后保存 |
| `session_search` | 搜索历史会话 | 查找之前的对话 |
| `session_save` | 保存聊天记录 | 会话结束时保存 |
| `knowledge_ingest` | 知识摄入 | 用户说"记住这个" |
| `knowledge_distill` | 知识蒸馏 | 将有价值对话转为知识 |
| `preflight_inject` | 知识预加载 | 会话开始时加载 |
| `guard_check` | 守护检查 | 检查请求是否与知识冲突 |
| `persona_summary` | 获取画像 | 了解用户偏好 |
| `health_check` | 健康检查 | 诊断系统状态 |

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

# 测试 MCP 连接
python -c "from integrations.agora import MCPServer; s = MCPServer(); print(len(s.tools))"
```

## 故障排查

### MCP 连接失败
1. 检查 `~/.config/opencode/opencode.json` 中的 MCP 配置路径
2. 确认 `mnemos_cli.py mcp serve` 可启动
3. 运行：`python3 mnemos_cli.py agent doctor opencode`

### 蒸馏任务不处理
1. 检查 `~/.opencode/mnemos_tasks/` 目录
2. 检查 `.mnemos_notify` 文件内容
3. 运行 `python -m mnemos_daemon status`
