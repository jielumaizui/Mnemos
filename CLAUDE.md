# Mnemos - AI 记忆、知识与决策辅助系统

## 系统概述

Mnemos 是一个本地优先的 AI 记忆、知识与决策辅助系统。它通过 Agent Kit、MCP、CLI、daemon 和本地 source parser 采集可授权的对话、文件和笔记信号，保留 raw 证据，按质量门与可信写入配置蒸馏为 Wiki/KG/搜索/画像/提醒等可消费资产，并在 AI 工作流中提供上下文、守护和推送。

当前代码层不是完整自治认知系统，也不是默认强制执行的可信推送决策系统。`trusted_push.mode=off` 保持旧写入，`shadow` 只生成 shadow proposal，`enforce` 才进入 ProposalQueue、PushDecisionGate、append-only WriteJournal 和 KnowledgeVaultWriter。当前没有本地 Web 控制中心；配置和运维以 CLI、MCP 工具和 `~/.mnemos/configs/main.json` 为准。

**核心设计原则：品质可控** - Mnemos 直接调用 LLM API 完成所有蒸馏任务，确保品质可控、流程闭环。宿主 Agent（Claude Code / Cursor / Copilot 等）负责触发和上下文注入，不执行蒸馏。

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     AI Agent (宿主)                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Session    │  │   MCP       │  │   触发调用          │  │
│  │  Hooks      │  │  Server     │  │   document_process  │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
└─────────┼────────────────┼────────────────────┼─────────────┘
          │                │                    │
          ▼                ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│                      Mnemos 核心层                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │  Sync     │ │Hephaestus│ │  KIA     │ │   Persona      │  │
│  │(同步采集) │ │(蒸馏Worker│ │(知识注入) │ │  (用户画像)     │  │
│  │           │ │  直接调API)│ │           │ │               │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └───────┬────────┘  │
│       │            │            │               │           │
│  ┌────┴────────────┴────────────┴───────────────┴────────┐  │
│  │                    Wiki (Obsidian Vault)                │  │
│  │  00-Inbox/  01-People/  02-Projects/  03-Tech/          │  │
│  │  04-Concepts/  05-MOCs/  06-Retrospectives/  07-Shadow/ │  │
│  │  99-Reports/                                             │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| **Olympus** | `integrations/olympus.py` | Agent 适配器注册中心，统一接口管理 |
| **Apollon** | `integrations/apollon.py` | Claude Code 适配器（Hooks、settings.json） |
| **Agent Sources** | `integrations/sources/*.py` | 多 Agent 数据源采集（Claude/Codex/Hermes/Kimi/Cursor/Gemini 等） |
| **EventBus** | `core/mnemos_bus.py` | 统一事件总线，跨 Agent 文件系统事件队列 |
| **Hephaestus** | `core/hephaestus/` | 蒸馏流水线：chunk → DistillBackend/LLM 提取 → 硬校验 → trusted_push/off-shadow-enforce 写入闭环 |
| **Trusted Push** | `core/trust/` | ProposalQueue、PushDecisionGate、EvidenceLedger、append-only WriteJournal、KnowledgeVaultWriter、MarkdownAdapter、Recovery 和白盒 DialogDecisionPush |
| **Hephaestus Worker** | `core/hephaestus_worker.py` | 蒸馏任务轮询 Worker 入口 |
| **KIA** | `core/kia/` | Knowledge Injection Agent，知识预加载、守护检查、注入 |
| **Persona** | `core/persona/` | 用户画像系统，信号采集 → 分析 → 盲区检测 → 校准 |
| **Sync** | `core/sync_framework/` | 统一同步采集框架，L1 原始对话存储 |
| **Agora** | `integrations/agora.py` | MCP 协议服务器，为 Agent 提供标准化工具接口 |
| **Chronos** | `core/kia/chronos.py` | 知识调度器，管理周期性知识复习和提醒 |
| **Hecate** | `core/kia/hecate.py` | 影子页面系统，联网搜索获取外部相关信息 |

## Agent 行为规范

> 通用行为规范见项目根目录 `AGENT_BEHAVIOR_POLICY.md`，适用于所有支持 Mnemos 的 Agent。
> 本文件（CLAUDE.md）为 Claude Code 专属上下文指令。

## MCP 工具（Agent 可用）

Mnemos 通过 MCP 协议向宿主 Agent 暴露以下工具。工具名以 `integrations/agora_tools/schema.py` 为权威来源：

MCP 身份不来自 tool arguments。宿主配置只持有 keyring reference，stdio 启动时由服务端解析不可变 `PrincipalEnvelope`；每次调用都经过 51/51 policy registry 和 capability 撤销/过期复验。不要发送 `agent/source_agent/allow_cross_agent/authorized_agents` 扩权参数；`session_id/project` 只能收窄已存在的服务端 grant。Wiki/raw/search ACL 缺失或冲突时必须拒绝，direct read 必须先授权 frontmatter 再读取正文，拒绝路径不得写热度、训练、画像、搜索会话、点击或提醒/推送历史。

### KIA 闭环

- **`preflight_inject`** - 任务前装载历史经验
  - 使用场景：用户开始新对话/任务时，自动加载相关背景知识
  - 参数：user_message, working_dir 等

- **`guard_check`** - 执行中风险守护
  - 使用场景：用户要求修改代码/配置时，检查是否与已有知识冲突
  - 参数：user_message, ai_response 等

- **`retrospective_list`** - 列出可用的 retrospective 经验
- **`check_pending_recaps`** - 检查待复盘事项，推动任务收尾

### 知识库操作

- **`wiki_search`** - 搜索 Wiki 知识库（多来源：文件导入、人工输入、raw vault、蒸馏、复盘、Git）
- **`wiki_read`** - 读取指定 Wiki 页面
- **`wiki_write`** - 写入 Wiki 页面
- **`wiki_build`** - 触发 Wiki 构建
- **`memory_write_project`** / **`memory_write_framework`** / **`memory_write_global`** - 写入项目/框架/全局级记忆
- **`memory_search`** - 按 project/framework/global 范围搜索记忆
- **`knowledge_source_list`** - 查看知识库来源分布统计

### 知识摄入与蒸馏

- **`knowledge_ingest`** - 用户主动口述知识 — 当用户说"记住这个"时调用
- **`knowledge_distill`** - 通过 LLM API 触发知识蒸馏
- **`document_process`** - 解析文档（文件蒸馏唯一入口）

### 会话捕获

- **`capture_turn`** - 逐轮上报对话
- **`capture_session`** - 批量上报整个 session
- **`end_session`** - 标记 session 结束
- **`capture_status`** - 查询 turn/session 处理状态
- **`session_search`** - 搜索历史会话

### 决策与搜索

- **`context_aware_search`** - 上下文感知搜索（画像加权 + 知识图谱召回）
- **`intent_route`** - 意图路由
- **`intent_correct`** - 记录用户确认后的真实意图
- **`blindspot_check`** - 盲区检测
- **`freshness_check`** - 知识新鲜度检查
- **`predictive_push`** - 预测性知识推送
- **`push_feedback`** - 对预测性推送反馈接受/忽略

### 画像与信号

- **`persona_summary`** - 获取用户画像摘要
- **`persona_behavior_prompt`** - 获取画像驱动的 AI 行为提示词
- **`persona_behavior_metrics`** - 获取画像行为指标
- **`persona_update`** - 触发画像更新
- **`signal_collect`** - 触发信号采集

### Observation & Reflection

- **`observation_run`** - 运行 Observation Engine
- **`observation_search`** - 搜索 Observation Index
- **`reflect_on_input`** - 基于输入自动触发 Reflection
- **`reflect_manually`** - 手动触发 Reflection
- **`reflection_feedback`** - 对 Reflection 提交反馈
- **`reflection_pending`** - 获取等待反馈的 Reflection 列表

### 系统

- **`health_check`** - 系统健康检查
- **`self_diagnose`** - Mnemos 自诊断
- **`configure_wiki`** - 配置 Wiki/Obsidian 路径
- **`detect_sources`** - 检测所有 Agent 数据源状态

## 自动触发机制

### 1. Session Start Hook（对话开始时）

由 `integrations/apollon.py` 安装到 Claude Code settings.json：

```json
{
  "hooks": {
    "SessionStart": "<当前 Python 解释器> integrations/apollon.py --session-start --working-dir ...",
    "SessionEnd": "<当前 Python 解释器> integrations/apollon.py --session-end --working-dir ..."
  }
}
```

触发时：
1. 收集当前工作目录的上下文信息
2. 调用 KIA 预加载相关知识
3. 检查 distill_queue 是否有待处理任务，如有则提示用户

### 2. Session End Hook（对话结束时）

触发时：
1. 将完整对话保存到 amphora SQLite 队列（默认 `~/.claude/distill_queue.db`）
2. Hephaestus Worker 从 amphora 拉取任务并蒸馏
3. Hephaestus Worker 检测到新任务后，直接调用 LLM API 执行蒸馏

### 3. Daemon 定时任务

`mnemos_daemon.py` 后台运行以下定时任务：
- **每 5 分钟**：收集已完成的蒸馏结果；`trusted_push.mode=off` 时按旧路径移入 Wiki，`shadow/enforce` 时先生成 Proposal/Gate 结果
- **每小时**：采集画像信号
- **每天 9:00**：检查知识调度到期任务

### 4. 文件监控

Hephaestus Worker 以纯轮询方式处理 amphora SQLite 蒸馏队列：
- 定时调用 `process_all()` 拉取 pending 任务
- 蒸馏完成后先过结构化/质量/认知门；`trusted_push.mode=enforce` 时拦截写页并进入 ProposalQueue，用户批准后由 append-only Journal + KnowledgeVaultWriter 写入
- 不再依赖文件系统 watchdog 触发

## 蒸馏执行机制

Mnemos 通过 `DistillBackend` 接口调用 LLM API 完成所有蒸馏任务，不委托给宿主 Agent。`LLMBackend` 是当前唯一生产 backend；本地 CLI `AgentBackend` 只允许通过 `mnemos agent shadow` + `mnemos golden eval --confirm-send-content` 做 shadow-only 评估，不能进入生产 `BackendChain`，不能绕过 Gate/Proposal/Journal/Writer，且任何发送到本地 agent subprocess 的内容都必须先过 `PromptSanitizer`。

**为什么不用 Agent 执行蒸馏**：
1. **品质不可控**：Agent 可能绕过 Mnemos 管道，自行处理文件，导致硬校验、知识图谱、Wiki 入库全部失效
2. **约定不可靠**：Agent 的自主行为无法强制约束，君子协定在复杂场景下必然被违反
3. **流程闭环**：只有 Mnemos 自己执行，才能保证从原始素材 → 蒸馏 → 硬校验 → 入库 → 知识图谱的完整闭环

**关键路径**（对话蒸馏）：
1. 对话结束 → amphora SQLite 蒸馏队列（`core.kia.amphora`）
2. Hephaestus Worker 检测到 → 调用 LLM API（主备链 failover）
3. 硬校验 → 通过后按 `trusted_push.mode` 决定旧写入、shadow proposal 或 enforce proposal；失败则存 distill_failed/
4. Charon 解析 → 分类归档到对应目录

**关键路径**（文件蒸馏）：
1. Agent 调用 `document_process(file_path)` MCP tool
2. Mnemos 解析文件内容 → 调用 LLM API 蒸馏
3. 硬校验 → 入库 Wiki/00-Inbox/
4. Charon 解析 → 分类归档

## 目录结构

```
~/.mnemos/
├── user_signals.db          # 画像信号数据库
├── configs/main.json        # 运行时权威配置
├── distill_output/          # 蒸馏输出（中间产物，Worker 处理完后删除）
├── locks/                   # 定时任务锁文件
├── logs/                    # 运行日志
└── calibrations/            # 画像校准记录

~/.claude/
├── distill_queue.db         # amphora SQLite 蒸馏任务队列（单一事实源，默认路径）
└── mnemos_distill_tasks/    # 代理任务提示

{wiki_dir}/
├── 00-Inbox/                # 蒸馏结果入口
├── 01-People/               # 人物/角色知识
├── 02-Projects/             # 项目知识
├── 03-Tech/                 # 技术知识
├── 04-Concepts/             # 概念/理论
├── 05-MOCs/                 # Map of Contents 导航页
├── 06-Retrospectives/       # 复盘与经验归档
├── 07-Shadow/               # 影子页面
└── 99-Reports/              # 系统报告输出
```

## 开发调试

### 常用命令

```bash
# 系统诊断
python3 -m mnemos_cli doctor

# 查看状态
python3 -m mnemos_cli status

# 启动守护进程
python3 -m mnemos_daemon start
python3 -m mnemos_daemon stop
python3 -m mnemos_daemon status

# 画像校准
python3 -m core.persona.calibration_cli

# 手动触发蒸馏
python3 -m core.hephaestus_worker

# 检查蒸馏队列
python3 -m mnemos_cli status

# 注册 Windows 开机启动
python3 -m mnemos_cli scheduler install-windows

# macOS / Linux 使用 daemon 模式（内置定时任务调度）
python3 -m mnemos_cli daemon start
```

### 测试

```bash
# 运行测试套件
pytest tests/ -v

# 验证模块导入
python3 -c "from core.config import get_config; print(get_config().wiki_dir)"
python3 -c "from integrations.agora import MCPServer; print(len(MCPServer().tools))"
```

### 配置项

运行时权威配置文件位于 `~/.mnemos/configs/main.json`。旧版 `~/.mnemos/config.yaml` 只作为迁移来源，不再是新部署配置入口：

```json
{
  "wiki": {
    "vault_path": "~/Documents/mnemos"
  },
  "storage": {
    "backend": "obsidian",
    "obsidian": {
      "vault_path": "~/Documents/raw"
    }
  },
  "llm": {
    "api_key_source": "env:MNEMOS_LLM_API_KEY",
    "base_url": "https://your-llm-api.example/v1",
    "model": "your-llm-model-id"
  },
  "embedding": {
    "api_key_source": "env:MNEMOS_EMBEDDING_API_KEY",
    "base_url": "https://your-embedding-api.example/v1",
    "model": "your-embedding-model-id"
  },
  "reranker": {
    "api_key_source": "env:MNEMOS_RERANKER_API_KEY",
    "base_url": "https://your-reranker-api.example/v1",
    "model": "your-reranker-model-id"
  }
}
```

三类模型端点在部署阶段必配并 smoke 验证；Reranker 的 `base_url` 可填写服务根地址或完整 `/rerank` endpoint。

## 跨平台支持

| 平台 | Daemon | Scheduler | Hooks | 文档处理 |
|------|--------|-----------|-------|----------|
| macOS | launchd + fork | launchd plist | 完整支持 | libreoffice/pdftotext |
| Linux | systemd (推荐) | cron/systemd timer | 完整支持 | libreoffice/pdftotext |
| Windows | subprocess (独立进程) | Task Scheduler | 完整支持 | 需安装 LibreOffice |

## 故障排查

### 蒸馏任务不处理

1. 检查 daemon 是否运行：`python3 -m mnemos_daemon status`
2. 检查 distill_queue 是否有任务：`python3 -m mnemos_cli status`
3. 检查 LLM API 配置是否正确：`python3 -m mnemos_cli config get llm`
4. 查看 distill_failed/ 目录是否有失败记录

### API 蒸馏失败

1. 检查 API Key 是否有效：`python3 -m mnemos_cli doctor`
2. 查看 `~/.mnemos/logs/` 中的错误日志
3. 检查主备 API 链配置（`llm.chain`，按顺序尝试并保留额外后备节点）

### 画像不更新

1. 检查信号数据库：`python3 -c "from core.persona.psyche import get_signal_store; print(get_signal_store().get_signal_stats())"`
2. 手动触发校准：`python3 -m core.persona.calibration_cli`
3. 检查数据源是否启用：`python3 -m mnemos_cli config`

## Health Stack

- typecheck: .venv/bin/python -m mypy --ignore-missing-imports core integrations daemon scripts mnemos_cli.py mnemos_daemon.py
- lint: .venv/bin/python -m flake8 core integrations daemon scripts mnemos_cli.py mnemos_daemon.py --count
- format: .venv/bin/python -m black --check --line-length=100 core integrations daemon scripts mnemos_cli.py mnemos_daemon.py
- test: .venv/bin/python -m pytest
- coverage: .venv/bin/python -m pytest tests/ --cov=core --cov=integrations --cov=mnemos_cli --cov=mnemos_daemon --cov-fail-under=70
- deadcode: .venv/bin/python -m vulture --exclude .venv,__pycache__,.git,.pytest_cache,.mypy_cache,.audit_venv,build,dist .
- shell: shellcheck setup.sh scripts/wiki_git_auto_commit.sh
