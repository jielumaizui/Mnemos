# Mnemos - AI 知识管理系统

## 系统概述

Mnemos 是一个全自动 AI 知识管理系统，负责将用户的 AI 对话、文件、笔记等原始素材自动提炼为结构化知识，并注入回 AI 的工作流中。

**核心设计原则：同源复用** - Mnemos 不直接调用任何 LLM API，所有需要 LLM 的任务都委托给宿主 Agent（Claude Code / Cursor / Copilot 等）处理。

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     AI Agent (宿主)                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Session    │  │   MCP       │  │   AgentDelegate     │  │
│  │  Hooks      │  │  Server     │  │   (同源复用)         │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
└─────────┼────────────────┼────────────────────┼─────────────┘
          │                │                    │
          ▼                ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│                      Mnemos 核心层                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │  Styx    │ │Hephaestus│ │  KIA     │ │   Persona      │  │
│  │(Memos集成)│ │(蒸馏Worker)│ │(知识注入) │ │  (用户画像)     │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └───────┬────────┘  │
│       │            │            │               │           │
│  ┌────┴────────────┴────────────┴───────────────┴────────┐  │
│  │                    Wiki (Obsidian Vault)                │  │
│  │  00-Inbox/  01-Projects/  02-Areas/  03-Resources/      │  │
│  │  04-Archives/  05-Periodic/  06-Memos/  07-Shadow/     │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| **Olympus** | `integrations/olympus.py` | Agent 适配器注册中心，统一接口管理 |
| **Apollon** | `integrations/apollon.py` | Claude Code 适配器（Hooks、settings.json） |
| **Caduceus** | `integrations/caduceus.py` | Hermes 适配器（Poll、Inbox 轮询） |
| **Typhon** | `integrations/typhon.py` | OpenClaw 适配器（SQLite、Hooks） |
| **Musae** | `integrations/musae.py` | OpenCode 适配器（JSON Config、Hooks） |
| **Daedalus** | `integrations/daedalus.py` | Codex 适配器（File-based、Windows .bat） |
| **EventBus** | `core/mnemos_bus.py` | 统一事件总线，跨 Agent 文件系统事件队列 |
| **AgentDelegate** | `core/prometheus_fire.py` | 蒸馏任务委托器，检测可用 Agent 并下发任务 |
| **Hephaestus** | `core/hephaestus_worker.py` | 蒸馏 Worker，轮询队列 → 委托 → 收集 → 验证 → 入库 |
| **KIA** | `core/kia/` | Knowledge Injection Agent，知识预加载、守护检查、注入 |
| **Persona** | `core/persona/` | 用户画像系统，信号采集 → 分析 → 盲区检测 → 校准 |
| **Styx** | `integrations/styx.py` | Memos API 客户端，同步对话记录 |
| **Agora** | `integrations/agora.py` | MCP 协议服务器，为 Agent 提供标准化工具接口 |
| **Chronos** | `core/kia/chronos.py` | 知识调度器，管理周期性知识复习和提醒 |
| **Hecate** | `core/kia/hecate.py` | 影子页面系统，联网搜索获取外部相关信息 |

## MCP 工具（Agent 可用）

Mnemos 通过 MCP 协议向宿主 Agent 暴露以下工具：

### 核心工具

- **`preflight_inject`** - 在对话开始时注入相关知识上下文
  - 使用场景：用户开始新对话时，自动加载相关背景知识
  - 参数：working_dir, user_message

- **`guard_check`** - 检查用户请求是否与已有知识冲突
  - 使用场景：用户要求修改代码/配置时，检查是否与最佳实践冲突
  - 参数：request_text, working_dir

- **`search_knowledge`** - 搜索 Wiki 知识库
  - 使用场景：需要查找历史知识、文档、经验时
  - 参数：query, limit

- **`log_signal`** - 记录用户行为信号（用于画像分析）
  - 使用场景：自动记录用户偏好和选择
  - 参数：dimension, value, confidence

### 画像工具

- **`get_persona_profile`** - 获取当前用户画像
  - 使用场景：了解用户的工作风格、认知偏好、价值取向
  - 返回：energy, cognitive, value 三层画像

- **`calibrate_persona`** - 启动画像校准流程
  - 使用场景：用户对画像推断结果有异议时

### 蒸馏工具

- **`distill_session`** - 手动触发对话蒸馏
  - 使用场景：当前对话结束后，将有价值内容提炼为知识
  - 参数：session_id, messages

- **`check_distill_queue`** - 检查待蒸馏任务队列
  - 使用场景：查看是否有积压的蒸馏任务

## 自动触发机制

### 1. Session Start Hook（对话开始时）

由 `integrations/apollon.py` 安装到 Claude Code settings.json：

```json
{
  "hooks": {
    "session_start": "python3 mnemos_cli.py --session-start --working-dir ...",
    "session_end": "python3 mnemos_cli.py --session-end --working-dir ..."
  }
}
```

触发时：
1. 收集当前工作目录的上下文信息
2. 调用 KIA 预加载相关知识
3. 检查 distill_queue 是否有待处理任务，如有则提示用户

### 2. Session End Hook（对话结束时）

触发时：
1. 将完整对话保存到 `~/.claude/distill_queue/`
2. 生成蒸馏任务 JSON 文件
3. Hephaestus Worker 检测到新任务后委托 Agent 处理

### 3. Daemon 定时任务

`mnemos_daemon.py` 后台运行以下定时任务：
- **每 5 分钟**：收集已完成的蒸馏结果，移入 Wiki Inbox
- **每小时**：采集画像信号
- **每天 9:00**：检查知识调度到期任务

### 4. 文件监控

如果 watchdog 可用，daemon 会实时监控 distill_queue 目录：
- 新 `.json` 文件出现时立即触发蒸馏处理
- 无需等待定时轮询

## 同源复用机制

Mnemos 不直接调用任何 LLM API。所有需要 LLM 的任务通过 `AgentDelegate` 委托：

```python
# core/prometheus_fire.py
class AgentDelegate:
    def delegate(self, task: DistillTask, output_path: Path) -> bool:
        # 1. 检测可用 Agent（Claude Code / Cursor / Copilot）
        # 2. 将任务写入文件，通知 Agent 处理
        # 3. Agent 读取任务，执行蒸馏，写入 output_path
        # 4. Hephaestus Worker 收集结果
```

**关键路径**：
1. 对话结束 → distill_queue/{session_id}.json
2. Hephaestus Worker 检测到 → 委托 Agent
3. Agent 处理 → ~/.mnemos/distill_output/{session_id}.md
4. Worker 收集 → Wiki/00-Inbox/
5. Charon 解析 → 分类归档到对应目录

## 目录结构

```
~/.mnemos/
├── user_signals.db          # 画像信号数据库
├── config.yaml              # 用户配置
├── locks/                   # 定时任务锁文件
├── logs/                    # 运行日志
└── calibrations/            # 画像校准记录

~/.claude/
├── distill_queue/           # 待蒸馏任务队列
│   ├── {session_id}.json    # 任务定义
│   └── {session_id}.delegated  # 已委托标记
├── distill_output/          # Agent 蒸馏输出
└── mnemos_distill_tasks/    # 代理任务提示

{wiki_dir}/
├── 00-Inbox/                # 蒸馏结果入口
├── 01-Projects/             # 项目知识
├── 02-Areas/                # 领域知识
├── 03-Resources/            # 资源库
├── 04-Archives/             # 归档
├── 05-Periodic/             # 周期性笔记
├── 06-Memos/                # Memos 同步
├── 07-Shadow/               # 影子页面
└── retrospectives/          # 复盘经验（KIA 预加载源）
```

## 开发调试

### 常用命令

```bash
# 系统诊断
python -m mnemos_cli doctor

# 查看状态
python -m mnemos_cli status

# 启动守护进程
python -m mnemos_daemon start
python -m mnemos_daemon stop
python -m mnemos_daemon status

# 画像校准
python -m core.persona.calibration_cli

# 手动触发蒸馏
python -m core.hephaestus_worker

# 检查蒸馏队列
python -m scripts.auto_distill --check

# 注册 Windows 开机启动
python -m mnemos_cli scheduler install-windows

# macOS / Linux 使用 daemon 模式（内置定时任务调度）
python -m mnemos_cli daemon start
```

### 测试

```bash
# 运行测试套件
pytest tests/ -v

# 验证模块导入
python -c "from core.config import get_config; print(get_config().wiki_dir)"
python -c "from integrations.agora import MCPServer; print(len(MCPServer().tools))"
```

### 配置项

配置文件位于 `~/.mnemos/config.yaml`：

```yaml
wiki:
  vault_path: ~/wiki  # Obsidian Vault 路径

memos:
  enabled: true
  api_url: https://memos.example.com
  token: ""  # 建议通过 MEMOS_TOKEN 环境变量设置

persona:
  enabled: true
  data_sources:
    session: {enabled: true}
    git: {enabled: true}
    file: {enabled: false}

integrations:
  claude_code:
    enabled: true
    settings_json_path: ~/Library/Application Support/Claude/settings.json
  mcp:
    enabled: true
```

## 跨平台支持

| 平台 | Daemon | Scheduler | Hooks | 文档处理 |
|------|--------|-----------|-------|----------|
| macOS | launchd + fork | launchd plist | 完整支持 | libreoffice/pdftotext |
| Linux | systemd (推荐) | cron/systemd timer | 完整支持 | libreoffice/pdftotext |
| Windows | subprocess (独立进程) | Task Scheduler | 完整支持 | 需安装 LibreOffice |

## 故障排查

### Agent 检测不到

1. 检查 `MNEMOS_HOST_AGENT` 环境变量是否设置
2. 运行 `python -m mnemos_cli agent detect`
3. 检查 Agent 的安装路径是否在 PATH 中

### 蒸馏任务不处理

1. 检查 daemon 是否运行：`python -m mnemos_daemon status`
2. 检查 distill_queue 是否有任务：`python -m scripts.auto_distill --check`
3. 检查 Agent 是否可用：`python -m mnemos_cli agent list`

### 画像不更新

1. 检查信号数据库：`python -c "from core.persona.psyche import get_signal_store; print(get_signal_store().get_signal_stats())"`
2. 手动触发校准：`python -m core.persona.calibration_cli`
3. 检查数据源是否启用：`python -m mnemos_cli config`
