# Mnemos

**AI Agent 的行为操作系统** —— 让你的 AI Agent 从"帮你记"升级到"帮你做"。

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/jielumaizui/mnemos/CI.yml?branch=main)](https://github.com/jielumaizui/mnemos/actions)

---

Mnemos 是一套面向 AI Agent 的个人知识管理闭环系统。它以 AI 对话为信号源，通过用户画像驱动知识装载和行为策略，实现知识的自动采集、蒸馏、应用和进化。

灵感源自 [Karpathy 的 LLM Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)——让 LLM 增量构建并维护持久化的知识库。Mnemos 在此基础上走了更远的一步：**知识不是存完就完了，它应该在行动中活着。**

## 它和别的"Second Brain"有什么不同？

| 维度 | 常见 Second Brain 工具 | Mnemos |
|------|----------------------|--------|
| 核心目标 | 知识存储与检索 | 知识在行动中被主动使用 |
| 知识流向 | 你 → 系统 → 你自己查 | 系统 → AI Agent → 实时辅助你 |
| 用户建模 | 无 | 三层画像雷达（能量/认知/价值），持续进化 |
| 质量保障 | 去重（如果有） | 五道 Guard + 三道 AI 自检 + 多源交叉验证 |
| 知识生命周期 | 手动管理或不管 | 热力评分 L0-L9 自动淘汰 |
| 行为驱动 | 无 | PreFlight 装载 + InProcess 守护 + Retrospective 复盘 |

简单说：**别人管"你存了什么"，Mnemos 管"AI 怎么用它帮你做事"。**

## 同源复用设计

Mnemos 本身**不直接调用任何 LLM API**。所有需要 LLM 的能力（如知识蒸馏、文档解析）都通过 **AgentDelegate** 委托给用户的本地 AI Agent 处理。

**设计原则：谁启动 Mnemos，蒸馏任务就委托给谁。**

Mnemos 支持 5 个宿主 Agent，通过统一的 `AgentAdapter` 接口接入：

| 适配器 | Agent | 机制 | 状态 |
|--------|-------|------|------|
| Apollon | Claude Code | Hooks (settings.json) | 完整支持 |
| Caduceus | Hermes | Poll + Inbox | 完整支持 |
| Typhon | OpenClaw | SQLite + Hooks | 完整支持 |
| Musae | OpenCode | JSON Config + Hooks | 完整支持 |
| Daedalus | Codex | File-based + Windows .bat | 完整支持 |

无 Agent 宿主时，Mnemos 自动扫描本地安装的 Agent，按优先级选择一个可用的执行蒸馏。

Mnemos 只负责：检测宿主 Agent → 将蒸馏任务打包为结构化 prompt → 通过文件协议写入任务目录 → 监控结果路径 → 验证 JSON 格式 → 拿到结果后转入 Wiki pipeline。

这个设计避免了：
1. **重复配置 API Key**：用户的 Agent 已经配置了 API Key，Mnemos 不需要再要一份
2. **模型版本不一致**：蒸馏用的模型和用户交互的模型保持一致
3. **供应商锁定**：支持任何遵循文件委托协议的 Agent

## 架构概览

```
原始资料层 → 蒸馏层 → 知识库层 → 应用层 (KIA) → 进化层 → 画像层
```

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Agent 适配器层 (Olympus)                            │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────┐ │
│  │ Apollon │ │Caduceus │ │ Typhon  │ │  Musae  │ │Daedalus│ │
│  │(Claude) │ │(Hermes) │ │(OpenClw)│ │(OpenCod)│ │(Codex) │ │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └───┬────┘ │
│       │           │           │           │           │      │
├───────┴───────────┴───────────┴───────────┴───────────┴──────┤
│  Layer 2: 统一事件总线 (Mnemos Event Bus)                      │
│  ~/.mnemos/events/  —  文件系统事件队列（跨进程/跨 Agent）    │
│  session.start | session.end | distill.request | signal.batch │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: Mnemos 核心服务（Agent-Agnostic）                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │Hephaestus│ │   KIA    │ │ Persona  │ │   Daemon       │  │
│  │(蒸馏Worker)│ │(知识注入) │ │(画像系统) │ │ (后台服务)      │  │
│  └──────────┘ └──────────┘ └──────────┘ └────────────────┘  │
│       │                                                     │
│       ▼                                                     │
│  ┌────────────────────────────────────────────────────────┐ │
│  │         Wiki 知识库 (Obsidian Vault)                    │ │
│  │  00-Inbox/  01-Projects/  02-Areas/  03-Resources/      │ │
│  │  04-Archives/  05-Periodic/  06-Memos/  07-Shadow/     │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## 核心概念

### Knowledge-in-Action (KIA) 闭环

知识不是静态存储的，而是在任务中被主动使用的：

1. **任务识别** —— 自动识别当前对话的任务类型
2. **知识装载** —— 根据任务类型和时间窗口，从 Wiki 装载历史经验
3. **过程守护** —— 实时检查用户行为是否符合历史教训，及时提醒
4. **自动复盘** —— 对话结束后，自动生成复盘笔记，沉淀新教训
5. **知识进化** —— 复盘结果驱动 Wiki 的迭代更新

### 用户画像三层雷达

从 AI 对话行为中推断用户的偏好画像：

- **能量模式**：专注深度、启动难度、续航模式、切换弹性
- **认知模式**：抽象/具象、系统/单点、质疑/信任、创造/优化
- **价值优先级**：正确/效率、深度/广度、完美/完成、创新/稳妥

画像不是静态标签，而是持续进化的。系统通过 A/B 测试验证画像的准确性，并通过盲区检测主动挑战用户的思维定势。

### 五道 Guard 防护

| Guard | 检测内容 | 处理方式 |
|-------|---------|---------|
| L0 内容去重 | content_hash 重复 | 跳过 |
| L3 引用污染 | wiki_ref 密度异常 | 仅创建 Source |
| L4 上下文回忆 | 回忆类内容 | 仅创建 Source |
| LQ 质量评估 | 5 维评分 < 40 | 隔离 |
| AI 三道自检 | 唯一性/严谨性/中立性 | 拒绝 |

### 热力评分 L0-L9

知识的活跃度追踪与自动淘汰：

| 区间 | 含义 | 读取深度 |
|------|------|---------|
| L0 | 沉睡 | 仅元数据 |
| L1-L3 | 温 | 摘要 100 字 |
| L4-L6 | 热 | 段落 500 字 |
| L7-L9 | 炽 | 全文 |

每次被 AI 检索到自动加温，长期不被引用自动降温。冷知识不删除，只是不再主动推荐。

## 5分钟快速上手

> 不用理解所有概念。跟着这个例子走一遍，你就知道 Mnemos 在做什么。

### 场景：你让 Claude 解决了一个 bug

**第1步：正常对话（你什么都不用做）**

你问 Claude："asyncio.gather 为什么内存爆炸？" 经过一番排查，找到了根因。对话结束。

**第2步：Hook 自动保存（系统做）**

Session End Hook 把对话写入 `~/.claude/distill_queue/abc123.json`，生成一个蒸馏任务。

**第3步：Daemon 委托 Agent（系统做）**

`HephaestusWorker` 检测到新任务，把完整的蒸馏 prompt 写入文件，通知 Claude："请把这段对话提炼成知识"。

**第4步：Agent 蒸馏（系统做）**

Claude 读取 prompt，输出结构化 JSON：
```json
{
  "judgment": "knowledge",
  "fragments": [{
    "form": "问题-解决",
    "title": "为什么 asyncio.gather 在大量任务下会内存爆炸",
    "core_content": "...根因分析...",
    "boundaries": {"applies": "高并发场景", "not_applies": "同步代码"}
  }]
}
```

**第5步：入库（系统做）**

Worker 验证格式通过后，生成 wiki 页面放入 `Wiki/00-Inbox/`：

```markdown
---
类型: 问题-解决
领域: 技术
复杂度: 进阶
置信度: 0.85
---

# 为什么 asyncio.gather 在大量任务下会内存爆炸

## 背景
排查 asyncio.gather 内存泄漏过程...

## 核心内容
...
```

**第6步：下次对话时自动使用（系统做）**

一周后你问 Claude："帮我写一个高并发爬虫"。

KIA 预加载扫描到这条知识，在对话开头注入：
> "你之前排查过 asyncio.gather 内存问题。这次涉及高并发，建议注意：..."

**全程你只做了一件事：正常对话。其余5步全自动。**

### 验证系统在工作

```bash
# 1. 检查蒸馏队列
python3 core/kia/amphora.py --list

# 2. 检查 daemon 状态
python3 -m mnemos_daemon status

# 3. 查看 Inbox 是否有新内容
ls ~/Documents/Obsidian\ Vault/wiki/00-Inbox/

# 4. 查看画像（有信号后自动生成）
cat ~/Documents/Obsidian\ Vault/wiki/01-People/user-persona.md
```

## 快速开始

### 前置条件

- Python >= 3.10
- 一个 AI Agent（Claude Code、Cursor、Continue 等）
- （可选）[Memos](https://github.com/usememos/memos) 实例 —— 原始资料入口
- （可选）[Obsidian](https://obsidian.md) —— 知识库可视化

> **注意**：本系统的设计前提是你已经在使用 AI Agent。没有 AI Agent，系统的核心价值（画像驱动、知识装载、过程守护）无法体现。

### 安装

```bash
# 方式一：pip 安装（推荐）
pip install mnemos
mnemos init
mnemos doctor

# 方式二：从源码安装
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
pip install -e ".[dev]"
cp config/config.example.yaml ~/.mnemos/config.yaml
# 编辑配置后
mnemos doctor
```

`mnemos doctor` 会自动检测系统状态，检查依赖是否就绪。

### 命令行工具

```bash
mnemos init                    # 交互式配置向导
mnemos doctor                  # 系统诊断
mnemos status                  # 查看系统状态
mnemos config                  # 查看/编辑配置
mnemos agent list              # 列出本地可用的 AI Agent
mnemos agent install           # 为所有可用 Agent 安装 hooks
mnemos agent doctor            # 诊断 Agent 状态
mnemos daemon start            # 启动后台守护进程
mnemos daemon stop             # 停止后台守护进程
mnemos daemon status           # 查看守护进程状态
mnemos scheduler install-windows   # 注册 Windows 开机启动
mnemos calibrate               # 启动画像校准流程
mnemos mcp serve               # 启动 MCP 服务器
```

## 与 AI Agent 集成

### 方式一：MCP 协议（推荐，通用）

任何支持 MCP 的 AI Agent 都可以接入。接入后，Agent 可以使用以下工具：

**知识库操作**
| 工具 | 用途 |
|------|------|
| `wiki_search` | 搜索知识库（多来源：文件导入、人工输入、Memos、蒸馏、复盘、Git） |
| `wiki_read` | 读取指定页面（经语义索引、热度评分、标签处理） |
| `wiki_write` | Agent 写入 Wiki 页面（蒸馏结果、生成的新知识） |
| `wiki_build` | 触发 Wiki 构建（L1→L2：扫描 Memos L1 记录 → 蒸馏 → 生成页面 → 索引更新） |
| `knowledge_source_list` | 查看知识库来源分布统计 |

**知识摄入**
| 工具 | 用途 |
|------|------|
| `knowledge_ingest` | **用户主动口述知识** — 当用户说"记住这个"时，Agent 调用此工具将知识写入 Memos |
| `knowledge_import` | **用户指定文件导入** — 当用户说"把这个文件存进知识库"时，Agent 读取文件并写入 Wiki，自动触发完整解析 |
| `knowledge_distill` | **触发知识蒸馏** — Agent 将聊天记录转为结构化 Wiki 知识（6 种形态），可选择直接写入 Wiki |
| `document_process` | **解析文档** — 处理 PDF/PPT/Excel/Word/HTML/EBOOK，提取结构和大纲，存入知识库 |

**会话管理**
| 工具 | 用途 |
|------|------|
| `session_save` | 保存完整聊天记录到 Memos（L1 原始池，按 hash/range/segment 分片，带完整性校验） |
| `session_search` | 搜索历史会话（自动合并分片，恢复完整对话） |

**KIA 闭环**
| 工具 | 用途 |
|------|------|
| `preflight_inject` | 任务前装载历史经验（KIA 闭环第一步） |
| `guard_check` | 执行中风险守护（KIA 闭环第二步） |
| `retrospective_list` | 列出可用的 retrospective 经验 |

**画像与信号**
| 工具 | 用途 |
|------|------|
| `persona_summary` | 获取用户画像摘要（能量/认知/价值三层雷达） |
| `persona_behavior_prompt` | 获取画像驱动的 AI 行为提示词 |
| `persona_update` | 触发画像更新（采集最新信号并重新计算） |
| `signal_collect` | 触发信号采集 |

**系统**
| 工具 | 用途 |
|------|------|
| `health_check` | 系统健康检查（配置、Memos 连通性、模块状态、文件统计） |

配置示例：

```json
{
  "mnemos": {
    "command": "mnemos",
    "args": ["mcp", "serve"]
  }
}
```

### 方式二：Claude Code Hooks

运行 `mnemos init` 时自动安装 hooks 到 `~/.claude/settings.json`。

### 方式三：Hermes Agent

Hermes Agent 通过 Poll 机制定期采集对话记录，无需额外配置。

## 与 Memos 和 Obsidian 的关系

Mnemos 与 [Memos](https://github.com/usememos/memos) 和 [Obsidian](https://obsidian.md) 是互补关系，不是替代。

### Memos：原始资料的入口

- Mnemos 通过 Memos SDK 连接你的 Memos 实例，采集原始笔记和对话记录
- Memos 负责**快速记录**，Mnemos 负责**深度蒸馏**
- **人工输入是重要入口**：当用户直接对 AI Agent 说"记住这个"、"帮我记下"时，Agent 通过 MCP 工具的 `knowledge_ingest` 将知识写入 Memos → 同步到 Wiki 00-Inbox/ → 经过 Charon 解析器（语义索引、实体提取、标签构建、热度评分 L0-L9）→ 正式进入知识图谱
- 所有进入系统的知识，无论来源（人工输入、Memos 同步、Agent 蒸馏、Git 历史），都经过同一套解析器处理，标准统一
- 你不需要 Memos 也能用 Mnemos（支持直接写入 Wiki 和其他数据源）
- Memos 是 [独立的开源项目](https://github.com/usememos/memos)，有自己的 [开源许可](https://github.com/usememos/memos/blob/main/LICENSE)，Mnemos 仅通过其公开 API 集成

### Obsidian：知识库的可视化与人工编辑

- Mnemos 的知识库层是**纯 Markdown + YAML Frontmatter**，不绑定任何特定工具
- 但强烈推荐使用 Obsidian：
  1. **原生兼容**：Obsidian 的笔记格式就是 Markdown，无需导出/转换
  2. **双向链接**：`[[页面名]]` 语法自动构建知识图谱
  3. **图谱视图**：Obsidian 的 Graph View 就是知识图谱可视化
  4. **社区生态**：Dataview、Templater 等插件可与 Mnemos 的数据联动
  5. **本地优先**：和 Mnemos 的数据隐私策略一致，所有知识库内容存本地
- Obsidian 是 [Obsidian Corp 的产品](https://obsidian.md)，Mnemos 不是 Obsidian 的插件或官方衍生品
- 配合方式：Obsidian 负责**知识的组织、可视化、人工编辑**；Mnemos 负责**知识的自动采集、蒸馏、画像驱动、闭环进化**。人管创作，AI 管运营。

### 数据所有权

- Mnemos 的知识库存储在你的本地磁盘（默认 `~/Documents/Obsidian Vault/wiki` 或自定义路径）
- 所有数据以纯 Markdown 文件形式存在，你可以随时用任何文本编辑器打开
- 画像数据存储在本地 SQLite 数据库中，不会上传到任何服务器
- Mnemos 不会收集、上传或分享你的任何数据

## 配置

配置文件位于 `~/.mnemos/config.yaml`（跨平台统一路径）。

关键配置项：

```yaml
wiki:
  # 知识库路径，留空则使用平台默认值
  vault_path: "~/Documents/Obsidian Vault/wiki"

memos:
  enabled: true
  api_url: "https://your-memos-instance.com"
  # token 建议通过 MEMOS_TOKEN 环境变量设置，不要明文写在配置文件中
  token: ""

persona:
  enabled: true
  data_sources:
    session: { enabled: true }       # AI对话信号（核心）
    git: { enabled: false }          # Git提交记录
    memos: { enabled: false }        # Memos笔记
    wiki: { enabled: false }         # 知识库交互
    file_system: { enabled: false }  # 文件系统活动
    wechat: { enabled: false }       # 微信聊天记录
```

## 数据源与隐私

用户画像的数据源完全由用户自选。开启越多画像越精准，但隐私暴露也越多：

| 数据源 | 用途 | 隐私级别 |
|--------|------|---------|
| AI 对话 | 推断专注深度、质疑倾向、完美偏好 | 仅本地存储 |
| Git 提交 | 推断续航模式、创新倾向 | 仅统计信息，不存代码 |
| Memos 笔记 | 推断认知风格、知识结构 | 过滤 AI 生成内容，仅存元数据 |
| Wiki 交互 | 推断关注领域、学习路径 | 仅页面路径和动作类型 |
| 微信聊天 | 推断情绪模式、社交偏好 | 仅本地处理，不上传 |

所有数据源默认关闭，用户需主动开启。微信数据源仅在本地处理，不涉及任何第三方服务。

## 技术栈

- **语言**：Python 3.10+
- **存储**：Markdown 文件（知识库）+ SQLite（画像与信号）
- **协议**：MCP (Model Context Protocol) 用于 AI Agent 集成
- **蒸馏委托**：AgentDelegate（同源复用，不直接调用 LLM API）
- **文档处理**：PDF / PPT / Excel / Word / HTML / EBOOK 解析
- **依赖**：requests、pyyaml（核心仅两个依赖）

## 项目状态

Mnemos 目前处于 **Beta** 阶段（v0.2.0）。核心架构和五 Agent 适配器已稳定。

### v0.2.0 新特性

- [x] **五 Agent 全适配**：Claude Code / Hermes / OpenClaw / OpenCode / Codex
- [x] **统一事件总线**：`core/mnemos_bus.py` 实现跨 Agent 事件通信
- [x] **蒸馏格式验证**：HephaestusWorker 增加 JSON 格式验证，无效输出自动移入 failed 目录
- [x] **Skip 智能过滤**：判定为 skip 的蒸馏结果不再污染 Inbox
- [x] **画像冷启动**：新用户自动获得默认画像模板，无需等待信号积累
- [x] **Windows 支持**：Task Scheduler 注册 (`mnemos scheduler install-windows`)
- [x] **画像校准 CLI**：`mnemos calibrate` 交互式校准流程
- [x] **蒸馏重试机制**：最大 3 次重试，超时任务自动恢复

### 长期能力

- [x] 三层架构：适配器层 → 事件总线 → 核心服务层
- [x] KIA 闭环：PreFlight 装载 + InProcess 守护 + Retrospective 复盘
- [x] 用户画像三层雷达（能量/认知/价值）+ 盲区检测
- [x] 同源复用设计（AgentDelegate，不直接调用 LLM API）
- [x] MCP 服务器（多工具覆盖知识库/摄入/会话/KIA/画像/系统）
- [x] CLI 工具（init / doctor / status / config / daemon / agent / calibrate / scheduler / mcp）
- [x] 文档处理（PDF/PPT/Excel/Word/HTML/EBOOK 解析入库）
- [ ] Web Dashboard
- [ ] Obsidian 插件
- [ ] 多语言画像模型

## 文档

- [架构说明](docs/ARCHITECTURE.md)
- [更新日志](docs/CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md)

## 致谢

- [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) —— LLM Wiki 模式的提出者，Mnemos 的核心灵感来源
- [Memos](https://github.com/usememos/memos) —— 优秀的开源笔记系统，Mnemos 的原始资料入口
- [Obsidian](https://obsidian.md) —— 知识管理的标杆工具，Mnemos 推荐的知识库可视化方案

## 许可证

[MIT License](LICENSE)

---

**Mnemos**（/ˈnɛmɒs/）—— 希腊神话中的记忆女神，谟涅摩叙涅。不是帮你记住，而是让你的 AI 懂得何时该想起。
