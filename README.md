# Mnemos

**Local Decision Brain & Behavior-Driven System**

> 全自动 AI Agent 知识决策系统 —— 不只是记住，而是让 AI 懂得何时该想起、如何该行动。
>
> 🌍 [English Version](README-en.md)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/jielumaizui/mnemos/CI.yml?branch=main)](https://github.com/jielumaizui/mnemos/actions)

---

**你是否也被这些问题困扰？**

- 和 AI 聊完一个复杂项目，两周后再问，它已经完全忘了之前的上下文
- 每次遇到同样的问题，都要重新搜索、重新踩一遍同样的坑
- 花了很多时间记笔记、整理文档，但真正需要的时候永远找不到
- 学了很多东西，过了一段时间就忘得一干二净
- 知道自己有很多知识盲区，但不知道盲区在哪里

**所有这些问题，本质上都是同一个问题：人类的认知能力是有限的。**

Mnemos 是一套全自动的 AI Agent 行为操作系统，它连接你所有的 AI 助手，完整记录每一次对话，自动从中提取结构化知识，构建你的专属知识图谱，然后在你需要的时候，主动把正确的知识推送给你。

**你不需要做任何额外的工作。** 不需要记笔记，不需要整理文档，不需要打标签，不需要搜索。你只需要正常地和 AI 聊天、正常工作，把文件交给 Mnemos，剩下的一切都全自动运行。

灵感源自 [Karpathy 的 LLM Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)——让 LLM 增量构建并维护持久化的知识库。Mnemos 在此基础上走了更远的一步：**知识不是存完就完了，它应该在决策中活着。**

## 它和别的"Second Brain"有什么不同？

| 维度 | 常见 Second Brain 工具 | Mnemos |
|------|----------------------|--------|
| 系统定位 | 知识存储与检索 | Local Decision Brain & Behavior-Driven System |
| 自动化程度 | 半自动（需手动整理/打标签） | 全自动（采集→评分→蒸馏→入库→决策，零手动操作） |
| 知识流向 | 你 → 系统 → 你自己查 | 系统 → AI Agent → 实时辅助决策 |
| 质量保障 | 去重（如果有） | 七层蒸馏流水线 + 自适应评分 + 三道自检 |
| 适应能力 | 规则固定 | 冷启动规则 → 贝叶斯自适应 → 行为反馈闭环 |
| 用户建模 | 无 | 三层画像雷达（能量/认知/价值），驱动决策策略 |
| 知识生命周期 | 手动管理或不管 | 评分驱动自动进化，过时知识主动预警，强制复盘闭环 |
| 模块耦合 | 一体化 | 热插拔设计，按需启用 |

## 核心竞争优势

### 存储是底线，不是卖点

知识存储和记忆检索是 Mnemos 最基础的功能。用户可以指定文件（PDF/Word/PPT/Excel/HTML 等）经蒸馏后进入知识库，AI 对话记录也会被全自动蒸馏为结构化知识——无需手动整理、无需手动打标签。但这些只是起点——**存下来不是目的，用起来才是。**

### 一、自适应动态调整引擎

系统不是一套写死的规则，而是一个全自动持续进化的判断机器：

- **三阶段冷启动**：COLD（纯规则）→ WARM（规则+贝叶斯混合）→ HOT（数据驱动），任何自适应模块在数据不足时都有规则兜底，不会因为"没数据"就罢工
- **贝叶斯评分**：每条知识、每个实体、每段关系都有置信度评分，新证据到来时实时更新后验概率
- **反馈闭环**：隐式信号（搜索/复制/停留时长）+ 显式反馈 → 加权融合 → 驱动评分模型重训练
- **漂移检测**：当特征分布偏移超过 3-sigma 时自动触发模型校准

评分引擎覆盖 6 个子系统：Memos 质量、同步优先级、蒸馏决策、知识图谱置信度、画像稳定性、运维健康度。每个子系统独立评分、独立进化、独立降级。

### 二、用户画像决策中枢

画像不是标签墙，而是决策中枢。系统从 AI 对话行为中推断用户的认知模式和价值取向，并将画像注入 AI 的工作流中：

- **三层雷达**：能量模式（专注/启动/续航/切换）、认知模式（抽象/系统/质疑/创造）、价值优先级（正确/效率/深度/完美/创新/自主）
- **画像驱动对话策略**：根据画像动态生成 AI 提示词片段，让 AI 的行为风格适配用户——完美主义者看到更严谨的建议，效率优先者看到更简洁的方案
- **双画像交叉验证**：行为画像 vs 知识画像，检测"言行不一"——嘴上说注重效率，行为上却在反复优化细节
- **情境隔离**：工作/个人/学习三种情境下的画像独立演进，避免跨界污染
- **14 维演化时间线**：长期追踪画像变化趋势，自动检测倦怠信号、认知转变和价值翻转

### 三、强制复盘与逻辑自检

知识入库不是终点，持续验证才是。系统全自动追踪知识生命周期，在关键时刻强制介入：

- **组合权重强制打开**：系统实时评估每条复盘待办的紧迫性——综合重要性（severity）、等待时长、同类问题频率、当前工作上下文关联度、承诺违约五个维度打分，达到阈值（≥4分）时自动打开 Obsidian 展示决策页面或复盘页面，确保你不会错过关键信号；未达阈值则仅对话内轻提醒，不打断工作流
- **用户预约直接弹开**：用户说"1天后提醒我复盘"，到点直接打开 Obsidian 对应的复盘页面，不走权重算法——你自己约的，系统不废话
- **启动补偿**：关机或合盖期间过期的预约，下次启动时自动补发——过期用户预约立即打开 Obsidian，过期系统提醒走权重判断
- **周报追达升级**：周报生成后 3 天内轻提醒，3 天后仍未读则强制插入对话，7 天后自动归档。配合 Wiki 看板徽章，100% 兜底
- **七层蒸馏流水线**：噪音过滤 → 价值预判 → LLM 判断 → 知识提取 → 自检验证 → 跨 Agent 关联 → 反馈循环。每一层都是一道过滤器，只有通过全部七层的知识才进入知识库
- **可证伪性标记**：每条知识标注可证伪条件和验证状态，过时知识不再沉默——系统会主动提醒"这条知识可能已过时"
- **争议仲裁**：当新知识与已有知识冲突时，不覆盖、不忽略，而是生成仲裁页面记录争议，等用户裁决
- **增量蒸馏 + 延迟蒸馏**：长对话每 5 轮增量生成草稿，低置信度内容进入延迟队列等信号充分后再处理
- **再循环守卫**：防止 Wiki 注入的内容被再次蒸馏回知识库，杜绝知识自引用污染

### 四、热插拔功能模块

Mnemos 的 14+ 子系统是可独立启停的功能模块，不是紧耦合的巨石。全自动运行，模块级故障自动隔离：

- **模块化架构**：每个子系统（知识图谱、影子页面、DNA 指纹、熵引擎、时间胶囊……）独立运行，关掉任何一个不影响核心链路
- **KIA 调度器**：16 步调度任务拓扑排序并行执行，单模块连续 3 次失败自动禁用，不拖垮全局
- **事件驱动**：模块间通过 EventBus 松耦合通信，蒸馏完成→图谱更新→画像刷新→推送评估，全链路异步
- **按需启用**：核心链路（同步→评分→蒸馏）开箱即用，高级功能（向量索引、预测推送、争议仲裁）按需开启

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│  应用层 — 决策输出                                               │
│  IntentRouter │ ApplicationHub │ ContextAwareSearch              │
│  PredictivePush │ BlindspotDiscovery │ DisputeResolver           │
│  FreshnessAlert │ WeeklyReport │ ForcedRetrospective              │
├─────────────────────────────────────────────────────────────────┤
│  知识层 — 理解与建模                                             │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ 知识图谱              │  │ 用户画像                          │  │
│  │ EntityManager         │  │ 三层雷达 + 交叉验证               │  │
│  │ RelationManager       │  │ 对话策略 + 情境隔离               │  │
│  │ EvolutionTracker      │  │ 14维演化时间线                    │  │
│  │ ContextAwareQuery     │  │ 盲区检测 + 校准                   │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  评分与蒸馏层 — 质量保障                                         │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ 自适应评分引擎        │  │ 七层蒸馏流水线                    │  │
│  │ COLD/WARM/HOT 三阶段  │  │ 噪音→预判→LLM→提取→自检→关联→反馈│  │
│  │ 6 子系统评分器         │  │ PromptBuilder + TokenBudget       │  │
│  │ 反馈闭环 + 漂移检测    │  │ 增量蒸馏 + 延迟蒸馏               │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  同步层 — 数据摄入                                               │
│  SyncEngine (8步流水线) │ 5 Agent Source │ FileIngestor          │
│  TriggerSystem (Watchdog/Polling/Hybrid) │ AgentLifecycleManager │
└─────────────────────────────────────────────────────────────────┘
```

## 同源复用设计

Mnemos 本身**不直接调用任何 LLM API**。所有需要 LLM 的能力（如知识蒸馏、文档解析）都通过 **AgentDelegate** 委托给用户的本地 AI Agent 处理。

**设计原则：谁启动 Mnemos，蒸馏任务就委托给谁。**

Mnemos 支持 6 个宿主 Agent，通过统一的 `AgentAdapter` 接口接入：

| 适配器 | Agent | 机制 | 状态 |
|--------|-------|------|------|
| Apollon | Claude Code | Hooks (settings.json) | 完整支持 |
| Caduceus | Hermes | Poll + Inbox | 完整支持 |
| Typhon | OpenClaw | JSONL + Hooks | 完整支持 |
| Musae | OpenCode | JSON Config + Hooks | 完整支持 |
| Daedalus | Codex | File-based + Windows .bat | 完整支持 |
| KimiAdapter | Kimi Code CLI | JSONL + Hooks | 完整支持 |

无 Agent 宿主时，Mnemos 自动扫描本地安装的 Agent，按优先级选择一个可用的执行蒸馏。

这个设计避免了：
1. **重复配置 API Key**：用户的 Agent 已经配置了 API Key，Mnemos 不需要再要一份
2. **模型版本不一致**：蒸馏用的模型和用户交互的模型保持一致
3. **供应商锁定**：支持任何遵循文件委托协议的 Agent

## 5 分钟快速上手

> 跟着这个例子走一遍，你就知道 Mnemos 在做什么。

### 场景：你让 Claude 解决了一个 bug

**第 1 步：正常对话**

你问 Claude："asyncio.gather 为什么内存爆炸？" 经过一番排查，找到了根因。对话结束。

**第 2 步：全自动蒸馏**

Session End Hook 自动触发蒸馏。对话内容全自动经过七层流水线处理——噪音过滤掉闲聊，价值预判识别出"这是有价值的排障经验"，LLM 提取为结构化知识，自检验证断言和代码片段，最终生成一条知识卡片。

**第 3 步：全自动评分入库**

自适应评分引擎对这条知识自动打分：质量评分 0.85，蒸馏评分 0.92。评分通过，知识卡片自动进入知识库。知识图谱同步创建实体和关系。

**第 4 步：全自动画像学习**

系统从这次对话中自动采集信号：你在排查时表现出高专注深度和质疑倾向，画像参数自动微调。下次类似场景，AI 会提前注入更严谨的排查建议。

**第 5 步：全自动主动决策**

一周后你开始写高并发爬虫。IntentRouter 自动识别任务意图，ContextAwareSearch 检索到之前的排障经验，画像决策中枢判断你应该会关心内存问题——主动在对话开头提醒你注意 asyncio.gather 的坑。

**全程你只做了一件事：正常对话。其余步骤全自动运行，零手动操作。**

### 验证系统在工作

```bash
# 1. 检查蒸馏队列
python3 core/kia/amphora.py --list

# 2. 检查 daemon 状态
python3 -m mnemos_daemon status

# 3. 查看 Inbox 是否有新内容
ls ~/Documents/Obsidian\ Vault/wiki/00-Inbox/

# 4. 查看画像
cat ~/Documents/Obsidian\ Vault/wiki/01-People/user-persona.md

# 5. 查看评分器状态
python3 mnemos_cli.py scorer status
```

## 🚀 30 秒快速开始

> 跟着走一遍，你就知道 Mnemos 在做什么。

### 前置条件

- Python >= 3.10
- 一个 AI Agent（Claude Code、Hermes、OpenClaw、OpenCode、Codex、Kimi 等）
- （可选）[Memos](https://github.com/usememos/memos) 实例
- （可选）[Obsidian](https://obsidian.md) 知识库可视化

> **注意**：Mnemos 不直接调用任何 LLM API，所有蒸馏任务都委托给你的 AI Agent 处理。没有 AI Agent，核心功能无法运行。

### 一键安装（推荐）

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
./setup.sh        # macOS / Linux
setup.bat         # Windows
```

`setup.sh` 会自动完成：
1. 检查 Python >= 3.10
2. 安装依赖
3. 自动检测 Memos 服务器（如有）
4. 自动检测 Obsidian Vault（如有）
5. 生成 `~/.mnemos/configs/main.json`
6. 初始化标准 wiki 目录结构
7. 安装 AI Agent hooks
8. 启动后台守护进程
9. 配置系统定时任务

全自动模式（无交互）：
```bash
./setup.sh --yes
```

跳过 Memos/Obsidian 检测：
```bash
./setup.sh --skip-memos --skip-obsidian
```

### 手动安装

如果你偏好手动配置：

```bash
# 1. 克隆并安装
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos

# 建议创建虚拟环境（避免 PEP 668 系统环境限制）
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. 复制并编辑配置
mkdir -p ~/.mnemos/configs
cp config/config.example.json ~/.mnemos/configs/main.json
# 编辑 ~/.mnemos/configs/main.json，设置你的 wiki 路径

# 3. 系统诊断
python3 mnemos_cli.py doctor
```

> **注意**：若使用 `setup.sh` 一键安装，依赖已安装在 `.venv` 中，后续命令请用 `.venv/bin/python mnemos_cli.py ...` 运行。

`mnemos doctor` 会自动检测系统状态，检查依赖是否就绪。

### 命令行工具

```bash
mnemos init                       # 交互式配置向导
mnemos doctor                     # 系统诊断
mnemos status                     # 查看系统状态
mnemos config                     # 查看/编辑配置

# Agent 管理
mnemos agent list                 # 列出本地可用的 AI Agent
mnemos agent install              # 为所有可用 Agent 安装 hooks
mnemos agent doctor               # 诊断 Agent 状态

# 后台服务
mnemos daemon start               # 启动后台守护进程
mnemos daemon stop                # 停止后台守护进程
mnemos daemon status              # 查看守护进程状态
mnemos scheduler install-windows  # 注册 Windows 开机启动

# 评分系统
mnemos scorer status              # 查看评分器状态和模式
mnemos scorer retrain             # 手动触发重训练
mnemos scorer rollback            # 回滚到上一版本模型

# 同步系统
mnemos sync status                # 查看同步状态
mnemos sync retry-failed          # 重试失败的同步任务

# 搜索与报告
mnemos search <query>             # 上下文感知搜索
mnemos report generate            # 生成每周画像报告

# 其他
mnemos calibrate                  # 启动画像校准流程
mnemos mcp serve                  # 启动 MCP 服务器
```

## 与 AI Agent 集成

### 方式一：MCP 协议（推荐，通用）

任何支持 MCP 的 AI Agent 都可以接入。接入后，Agent 可以使用以下工具：

**知识库操作**

| 工具 | 用途 |
|------|------|
| `wiki_search` | 搜索知识库（多来源：文件导入、人工输入、Memos、蒸馏、复盘、Git） |
| `wiki_read` | 读取指定页面（经语义索引、评分、标签处理） |
| `wiki_write` | Agent 写入 Wiki 页面（蒸馏结果、生成的新知识） |
| `wiki_build` | 触发 Wiki 构建（扫描 → 蒸馏 → 生成页面 → 索引更新） |
| `knowledge_source_list` | 查看知识库来源分布统计 |

**知识摄入**

| 工具 | 用途 |
|------|------|
| `knowledge_ingest` | 用户主动口述知识 — 当用户说"记住这个"时调用 |
| `knowledge_import` | 用户指定文件导入 — 解析后写入 Wiki |
| `knowledge_distill` | 触发知识蒸馏 — 聊天记录转为结构化 Wiki 知识 |
| `document_process` | 解析文档 — PDF/PPT/Excel/Word/HTML/EBOOK |

**会话管理**

| 工具 | 用途 |
|------|------|
| `session_save` | 保存完整聊天记录到 Memos（分片存储 + 完整性校验） |
| `session_search` | 搜索历史会话（自动合并分片，恢复完整对话） |

**KIA 闭环**

| 工具 | 用途 |
|------|------|
| `preflight_inject` | 任务前装载历史经验（KIA 闭环第一步） |
| `guard_check` | 执行中风险守护（KIA 闭环第二步） |
| `retrospective_list` | 列出可用的 retrospective 经验 |

**决策与搜索**

| 工具 | 用途 |
|------|------|
| `context_aware_search` | 上下文感知搜索（画像加权 + 知识图谱召回） |
| `intent_route` | 意图路由（自动分类：回忆/知识/任务/闲聊） |
| `blindspot_check` | 盲区检测（检查知识库覆盖缺口） |
| `freshness_check` | 知识新鲜度检查（版本绑定 + 过时预警） |

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
| `health_check` | 系统健康检查（配置、Memos 连通性、模块状态） |
| `predictive_push` | 预测性知识推送（基于当前上下文主动推荐） |

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

### 方式三：Hermes / OpenClaw / OpenCode / Codex / Kimi Agent

各 Agent 通过各自的适配器机制（Poll / JSONL / JSON Config / File-based）自动采集对话记录，无需额外配置。

## 与 Memos 和 Obsidian 的关系

Mnemos 与 [Memos](https://github.com/usememos/memos) 和 [Obsidian](https://obsidian.md) 是互补关系，不是替代。

### Memos：原始资料的入口

- Mnemos 通过 Memos SDK 连接你的 Memos 实例，采集原始笔记和对话记录
- Memos 负责**快速记录**，Mnemos 负责**深度蒸馏与决策驱动**
- **人工输入是重要入口**：当用户对 AI Agent 说"记住这个"时，Agent 通过 `knowledge_ingest` 将知识写入 Memos → 同步到 Wiki → 经过评分和蒸馏 → 正式进入知识图谱
- 所有进入系统的知识，无论来源（人工输入、Memos 同步、Agent 蒸馏、文件导入、Git 历史），都经过同一套评分与蒸馏管线处理，标准统一
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
- 配合方式：Obsidian 负责**知识的组织、可视化、人工编辑**；Mnemos 负责**知识的自动采集、蒸馏、评分、画像驱动、闭环进化**。人管创作，AI 管运营。

### 数据所有权

- Mnemos 的知识库存储在你的本地磁盘（默认 `~/Documents/Obsidian Vault/wiki` 或自定义路径）
- 所有数据以纯 Markdown 文件形式存在，你可以随时用任何文本编辑器打开
- 画像数据存储在本地 SQLite 数据库中，不会上传到任何服务器
- Mnemos 不会收集、上传或分享你的任何数据

## 配置

运行时权威配置文件位于 `~/.mnemos/configs/main.json`（跨平台统一路径）。
旧版 `~/.mnemos/config.yaml` 仅作为迁移来源，系统会在首次读取时迁移到 JSON。

关键配置项：

```json
{
  "wiki": {
    "vault_path": "~/Documents/Obsidian Vault/wiki"
  },
  "memos": {
    "enabled": true,
    "api_url": "https://your-memos-instance.com",
    "token": ""
  },
  "persona": {
    "enabled": true,
    "data_sources": {
      "session": { "enabled": true },
      "git": { "enabled": false },
      "memos": { "enabled": false },
      "wiki": { "enabled": false },
      "file_system": { "enabled": false }
    }
  },
  "daemon": {
    "services": {
      "capture_worker": true,
      "l1_sync": false,
      "event_bus": true
    }
  }
}
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
- **存储**：Markdown 文件（知识库）+ SQLite（画像/评分/图谱/调度）
- **协议**：MCP (Model Context Protocol) 用于 AI Agent 集成
- **蒸馏委托**：AgentDelegate（同源复用，不直接调用 LLM API）
- **评分算法**：ComplementNB + TfidfVectorizer + 贝叶斯后验更新
- **聚类算法**：HDBSCAN → DBSCAN → K-Means 回退链
- **调度**：拓扑排序 + ThreadPoolExecutor 并行执行
- **文档处理**：PDF / PPT / Excel / Word / HTML / EBOOK 解析
- **核心依赖**：requests、pyyaml、watchdog、numpy

## 项目状态

Mnemos v2.0.0 正式版。

### v2.0.0 重大更新

- [x] **自适应评分引擎**：COLD/WARM/HOT 三阶段 + 6 子系统评分器 + 反馈闭环
- [x] **七层蒸馏流水线**：噪音过滤 → 价值预判 → LLM 判断 → 知识提取 → 自检 → 跨 Agent 关联 → 反馈循环
- [x] **知识图谱扩展**：EntityManager + RelationManager + 贝叶斯置信度更新 + 上下文感知查询
- [x] **画像决策中枢**：三层雷达 + 交叉验证 + 对话策略 + 情境隔离 + 14 维演化时间线
- [x] **应用层**：IntentRouter + ApplicationHub + ContextAwareSearch + 预测推送 + 争议仲裁 + 知识新鲜度预警
- [x] **同步框架完善**：SyncEngine 8 步流水线 + 5 Agent Source + 触发系统 + 文件摄入
- [x] **KIA 调度器重构**：16 步拓扑排序并行调度 + 事件驱动 + 自动禁用故障模块
- [x] **增量与延迟蒸馏**：长对话增量生成草稿 + 低置信度内容延迟队列 + 碎片合并
- [x] **PromptBuilder**：Token 预算管理 + 模板系统 + 相关上下文组装
- [x] **配置系统升级**：三层优先级（代码默认 < JSON 配置 < 环境变量）

### 长期能力

- [x] 同源复用设计（AgentDelegate，不直接调用 LLM API）
- [x] 六 Agent 全适配（Claude Code / Hermes / OpenClaw / OpenCode / Codex / Kimi）
- [x] MCP 服务器（多工具覆盖知识库/摄入/会话/KIA/画像/决策/系统）
- [x] 文档处理（PDF/PPT/Excel/Word/HTML/EBOOK 解析入库）
- [x] 热插拔模块化架构（14+ 子系统独立启停）
- [ ] 向量索引（hnswlib + Embedding API）
- [ ] Web Dashboard
- [ ] Obsidian 插件

## 致谢

- [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) —— LLM Wiki 模式的提出者，Mnemos 的核心灵感来源
- [Memos](https://github.com/usememos/memos) —— 优秀的开源笔记系统，Mnemos 的原始资料入口
- [Obsidian](https://obsidian.md) —— 知识管理的标杆工具，Mnemos 推荐的知识库可视化方案

## 许可证

[MIT License](LICENSE)

---

**Mnemos**（/ˈnɛmɒs/）—— 希腊神话中的记忆女神，谟涅摩叙涅。不是帮你记住，而是让你的 AI 全自动地懂得何时该想起、如何该行动。
