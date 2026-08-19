# Mnemos

**Local Decision Brain & Behavior-Driven System**

> 本地优先的 AI Agent 记忆、知识与决策辅助系统 —— 不只是记住，而是让 AI 懂得何时该想起、如何该行动。
>
> 当前版本 v2.0.0：核心链路（采集 → 蒸馏 → 入库 → 辅助决策）已可用；自适应评分、主动推送精准度等高级能力随数据积累持续优化。
>
> 🌍 [English Version](README-en.md)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/jielumaizui/mnemos/ci.yml?branch=main)](https://github.com/jielumaizui/mnemos/actions)

---

**你是否也被这些问题困扰？**

- 和 AI 聊完一个复杂项目，两周后再问，它已经完全忘了之前的上下文
- 每次遇到同样的问题，都要重新搜索、重新踩一遍同样的坑
- 花了很多时间记笔记、整理文档，但真正需要的时候永远找不到
- 学了很多东西，过了一段时间就忘得一干二净
- 知道自己有很多知识盲区，但不知道盲区在哪里

**所有这些问题，本质上都是同一个问题：人类的认知能力是有限的。**

Mnemos 是一套面向本地运行的 AI Agent 记忆、知识和决策辅助系统。它连接你所有的 AI 助手，完整记录每一次对话，自动从中提取结构化知识，构建你的专属知识图谱和用户画像，然后在你需要的时候，主动把正确的知识推送回 AI 的工作流。

**你不需要做任何额外的整理。** 不需要记笔记，不需要打标签，不需要搜索。你只需要正常地和 AI 聊天、正常工作，把文件交给 Mnemos，剩下的采集、蒸馏、评分、入库、推送全部自动运行。

> **诚实的边界**：v2.0.0 不是完整自治的认知系统。高风险写入、可信推送 enforce 模式、数据删除和部分修复动作仍需要你的显式授权；系统也没有 Web 控制中心，一切通过 CLI、MCP、配置文件和 Obsidian 完成。

灵感源自 [Karpathy 的 LLM Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)——让 LLM 增量构建并维护持久化的知识库。Mnemos 在此基础上增加了决策辅助层：**知识不是存完就完了，它应该在决策中活着。**

## 它和别的"Second Brain"有什么不同？

| 维度 | 常见 Second Brain 工具 | Mnemos |
|------|----------------------|--------|
| 系统定位 | 知识存储与检索 | 本地优先的 AI 记忆、知识与决策辅助系统 |
| 自动化程度 | 半自动（需手动整理/打标签） | 自动采集 → 蒸馏 → 入库 → 推送，零手动整理 |
| 知识流向 | 你 → 系统 → 你自己查 | 系统 → AI Agent → 实时辅助决策 |
| 质量保障 | 去重（如果有） | 七层蒸馏流水线 + 通用质量门 + 认知价值门 + 自适应评分 |
| 适应能力 | 规则固定 | 冷启动规则 → 贝叶斯自适应 → 行为反馈闭环 |
| 用户建模 | 无 | 用户认知画像（三层雷达 + 可反驳画像断言 + 消费效果日志），驱动决策策略 |
| 知识生命周期 | 手动管理或不管 | 评分驱动自动进化，过时知识主动预警，强制复盘闭环 |
| 写入安全 | 直接写库 | 可选可信推送闭环：ProposalQueue → 审批 → append-only Journal → 受控写入 |
| 模块耦合 | 一体化 | 热插拔设计，按需启用 |

## 核心竞争优势

### 存储是底线，不是卖点

知识存储和记忆检索是 Mnemos 最基础的功能。你可以把文件（PDF/Word/PPT/Excel/HTML/EBOOK）交给它蒸馏入库，AI 对话记录也会被自动采集并蒸馏为结构化知识——无需手动整理、无需手动打标签。但这些只是起点——**存下来不是目的，用起来才是。**

### 一、自适应动态调整引擎

系统不是一套写死的规则，而是一个持续进化的判断机器：

- **三阶段冷启动**：COLD（纯规则）→ WARM（规则+贝叶斯混合）→ HOT（数据驱动），任何自适应模块在数据不足时都有规则兜底，不会因为"没数据"就罢工
- **贝叶斯评分**：每条知识、每个实体、每段关系都有置信度评分，新证据到来时实时更新后验概率
- **反馈闭环**：隐式信号（搜索/点击/忽略）+ 显式反馈 → 加权融合 → 驱动评分模型重训练
- **漂移检测**：当特征分布偏移超过阈值时自动触发模型校准

评分引擎覆盖 5 个域评分器（同步、raw 采集、知识图谱、画像、运维健康）+ 1 个独立蒸馏评分器。每个域独立评分、独立进化、独立降级。自适应策略矩阵覆盖蒸馏、质量门、评分、投递、搜索、文档处理等 9 个域；shadow 实验保留 24 小时回滚窗口，没有 active shadow 时严格尊重你的配置。

### 二、用户画像决策中枢

画像不是标签墙，而是决策中枢。系统从 AI 对话行为中推断你的认知模式和价值取向，并将画像注入 AI 的工作流中：

- **三层雷达**：能量模式（专注/启动/续航/切换）、认知模式（抽象/系统/质疑/创造）、价值优先级（正确/效率/深度/完美/创新/自主）
- **可反驳的画像断言**：纠错、忽略、打断、返工和明确偏好会沉淀为带证据、置信度、隐私等级的画像断言——低置信或被你纠正的断言必须能被后续证据反驳或撤销
- **画像驱动对话策略**：根据画像动态生成 AI 提示词片段，让 AI 的行为风格适配你——完美主义者看到更严谨的建议，效率优先者看到更简洁的方案
- **消费效果闭环**：preflight、搜索、蒸馏、质量门消费画像后都会记录"用到哪些断言、是否改变了行为、结果如何"，画像不再自说自话
- **情境隔离**：工作/个人/学习三种情境下的画像独立演进，避免跨界污染
- **14 维演化时间线**：长期追踪画像变化趋势，自动检测倦怠信号、认知转变和价值翻转

### 三、强制复盘与逻辑自检

知识入库不是终点，持续验证才是。系统按预算、权重和你的确认策略追踪知识生命周期，在关键时刻提醒或请求介入：

- **组合权重强制打开**：系统实时评估每条复盘待办的紧迫性——综合重要性、等待时长、同类问题频率、当前上下文关联度、承诺违约五个维度打分，达到阈值时自动打开 Obsidian 展示决策/复盘页面；未达阈值则仅对话内轻提醒，不打断工作流
- **用户预约直接弹开**：你说"1 天后提醒我复盘"，到点直接打开 Obsidian 对应页面，不走权重算法——你自己约的，系统不废话
- **启动补偿**：关机或合盖期间过期的预约，下次启动时自动补发
- **复盘真正被消费**：复盘结论经 plans → commands → receipts 的持久化扇出，真正落到检索、策略补丁、画像、调度和评分里；负反馈可以撤销已提交的效果——复盘不再是写完就没人看的文档
- **七层蒸馏流水线**：噪音过滤 → 价值预判 → LLM 判断 → 知识提取 → 自检验证 → 跨 Agent 关联 → 反馈循环。写入 Wiki 前还要经过通用质量门和认知价值门：页面必须说明它对决策、方法、反模式或偏好的认知贡献
- **争议仲裁**：当新知识与已有知识冲突时，不覆盖、不忽略，而是生成仲裁页面记录争议，等你裁决
- **增量蒸馏 + 延迟蒸馏**：长对话每 5 轮增量生成草稿，低置信度内容进入延迟队列等信号充分后再处理
- **再循环守卫**：防止 Wiki 注入的内容被再次蒸馏回知识库，杜绝知识自引用污染

### 四、可信写入与热插拔模块

- **可信推送闭环（可选）**：`trusted_push.mode=off|shadow|enforce`。enforce 模式下，蒸馏写页必须先进入 ProposalQueue，经你审批后才由 append-only WriteJournal 和受控 Writer 落盘——AI 不能悄悄改写你的知识库，所有写入可审计、可回滚
- **Wiki 投影生命周期**：正式 Wiki 页的每次 create/update/move/delete 先落 append-only mutation 账本，再向知识图谱、认知图、关系向量、搜索索引、页面度量、MOC 导航六个消费者发布事件并分别收回执——Wiki 与派生索引再也不会悄悄脱节
- **模块化架构**：知识图谱、影子页面、DNA 指纹、熵引擎、时间胶囊等 14+ 子系统独立运行，关掉任何一个不影响核心链路
- **KIA 调度器**：16 步调度任务拓扑排序并行执行，单模块连续失败自动禁用，不拖垮全局
- **事件驱动**：模块间通过 EventBus 松耦合通信，蒸馏完成 → 图谱更新 → 画像刷新 → 推送评估，全链路异步
- **资源治理**：`ResourceBudget` 动态监测 CPU/内存/温度/电源，高温或电池供电时后台任务自动降速而非关闭

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│  应用层 — 决策输出                                               │
│  IntentRouter │ ContextAwareSearch │ PredictivePush              │
│  BlindspotDiscovery │ DisputeResolver │ ForcedRetrospective      │
│  FreshnessAlert │ PolicyPatch │ DeliveryRouter                   │
├─────────────────────────────────────────────────────────────────┤
│  认知层 — 观察与反思（L3/L4/L5）                                  │
│  ObservationEngine（行为观察） │ ReflectionEngine（偏差检测+洞察） │
│  FeedbackLoop（反馈归因与撤销） │ CognitiveGraph（跨层认知图）      │
├─────────────────────────────────────────────────────────────────┤
│  知识层 — 理解与建模                                             │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ 知识图谱              │  │ 用户画像                          │  │
│  │ EntityManager         │  │ 三层雷达 + 可反驳断言             │  │
│  │ RelationManager       │  │ 对话策略 + 情境隔离               │  │
│  │ EvolutionTracker      │  │ 14 维演化时间线                   │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  评分与蒸馏层 — 质量保障                                         │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ 自适应评分引擎        │  │ 七层蒸馏流水线（Hephaestus）       │  │
│  │ COLD/WARM/HOT 三阶段  │  │ 噪音→预判→LLM→提取→自检→关联→反馈│  │
│  │ 5 域评分器+蒸馏评分器 │  │ 质量门 + 认知价值门               │  │
│  │ 反馈闭环 + 漂移检测    │  │ 增量蒸馏 + 延迟蒸馏               │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  可信写入层 — 可审计的变更                                       │
│  ProposalQueue │ PushDecisionGate │ WriteJournal（append-only）  │
│  WikiProjectionLedger │ ActionLedger │ SnapshotManager           │
├─────────────────────────────────────────────────────────────────┤
│  同步层 — 数据摄入                                               │
│  Capture（MCP 上报） │ SyncEngine 8 步流水线 │ 12 个 Agent Source │
│  DocumentImport（PDF/Word/PPT/Excel/HTML/EBOOK） │ FileIngestor  │
├─────────────────────────────────────────────────────────────────┤
│  守护进程 — 38 个后台服务                                        │
│  raw_sync │ distill_and_merge │ persona_analyzer │ scheduler_tick│
│  observation_engine │ reflection_engine │ recap_consumption │ …  │
└─────────────────────────────────────────────────────────────────┘
```

## 系统怎么跑起来（6 步链路）

Mnemos 的核心价值链路可以概括为 **采集 → 同步 → 投影 → 蒸馏 → 入库 → 辅助决策**：

```
1. 采集（Capture）
   各 Agent 对话结束 / MCP 上报 / 用户导入文件
        ↓
2. 同步（Sync）
   SyncEngine 将原始内容标准化，写入 append-only raw revision
        ↓
3. 展示投影（Raw Projection）
   daemon 将 current revision 投影为可读的 raw/<agent>/<date>/<chunk>.md
        ↓
4. 蒸馏（Distill）
   Hephaestus 七层流水线把原始材料提炼成结构化 Wiki 页面
        ↓
5. 入库 + 建图谱（Store & Graph）
   默认模式直接写入 Wiki；enforce 模式先经 ProposalQueue 审批
        ↓
6. 辅助决策（KIA）
   Preflight 预加载、Guard 守护、PredictivePush 主动推送、强制复盘闭环
```

全程使用持久化、revision-aware 的 typed receipt 闭环：采集只有拿到入队回执才算完成；蒸馏只有形成正式页面或明确的 intentional skip 才进入终态；partial、retry 和写失败都保持非终态、可恢复。

## 蒸馏执行模型

Mnemos **通过 `DistillBackend` 接口直接调用 LLM API** 完成蒸馏，确保品质可控、流程闭环。生产默认实现是 `LLMBackend`（OpenAI-compatible HTTP caller）；本地 CLI `AgentBackend` 只允许在 shadow-only 评估面运行，不进入生产写入链路。

**设计原则：蒸馏执行权在 Mnemos，不在 Agent。**

为什么不委托给 Agent 执行蒸馏：

1. **品质不可控** — Agent 可能绕过 Mnemos 管道自行处理，导致硬校验、知识图谱构建、Wiki 入库全部失效
2. **约定不可靠** — Agent 的自主行为无法强制约束，"君子协定"必然被违反
3. **流程闭环** — 只有 Mnemos 自己执行，才能保证原始素材 → 蒸馏 → 硬校验 → 入库 → 知识图谱的完整闭环

Mnemos 通过 **LLMApiChain** 实现有序 failover（主备/同厂商/跨厂商链式回退），不绑定任何模型厂商——只要端点兼容 OpenAI 风格 API 即可。

## 5 分钟快速上手

> 跟着这个例子走一遍，你就知道 Mnemos 在做什么。

### 场景：你让 Claude 解决了一个 bug

**第 1 步：正常对话**

你问 Claude："asyncio.gather 为什么内存爆炸？"经过一番排查，找到了根因。对话结束。

**第 2 步：自动触发蒸馏**

Session 结束信号自动触发蒸馏。对话内容经过七层流水线处理——噪音过滤掉闲聊，价值预判识别出"这是有价值的排障经验"，LLM 提取为结构化知识，自检验证断言和代码片段，再由通用质量门和认知价值门确认它不是普通参考文本，最终生成一条知识卡片。

**第 3 步：评分与受控入库**

自适应评分引擎对这条知识自动打分。评分与认知贡献门通过后，默认模式直接写入知识库；若启用 `trusted_push.mode=enforce`，知识卡片会先进入 ProposalQueue，待你审批后写入。知识图谱同步创建实体和关系。

**第 4 步：画像学习**

系统从这次对话中自动采集信号：你在排查时表现出高专注深度和质疑倾向，也可能明确纠正"先测试再提交"。这些内容会沉淀为可反驳的画像断言。下次类似场景，preflight/搜索/蒸馏/质量门会消费这些断言，并记录它是否真的改变了行动。

**第 5 步：主动辅助决策**

一周后你开始写高并发爬虫。IntentRouter 自动识别任务意图，ContextAwareSearch 检索到之前的排障经验，画像决策中枢判断你应该会关心内存问题——主动在对话开头提醒你注意 asyncio.gather 的坑。

**全程你只做了一件事：正常对话。**

### 验证系统在工作

```bash
# 1. 检查蒸馏队列
python3 -m core.kia.amphora --list

# 2. 检查 daemon 状态
python3 mnemos_cli.py daemon status

# 3. 查看 Inbox 是否有新内容（默认 Wiki 路径）
ls ~/Documents/mnemos/00-Inbox/

# 4. 查看画像
cat ~/Documents/mnemos/L5-Feedback/user-persona.md

# 5. 查看评分器状态
python3 mnemos_cli.py scorer status
```

## 🚀 快速开始

### 前置条件

- Python >= 3.10
- 一个 AI Agent（Claude Code、Kimi、Crush、Codex、Hermes、Kiro、OpenCode、OpenClaw 任一即可）
- **必装** [Obsidian](https://obsidian.md)：raw Vault 保存原始对话，Mnemos Vault 保存蒸馏后的知识库，两者都需要能被 Obsidian 打开并人工核验；安装时未检测到 Obsidian 会停止并说明原因
- **必配** 三类模型端点：LLM（对话/蒸馏）、Embedding（向量/语义召回）、Reranker（搜索重排）。每类都需要模型 ID、API 地址和 API Key
- **可选** 多模态模型端点：用于图片、截图和视觉证据解析；不配置不影响正常使用

> Mnemos 不绑定模型厂商。只要端点兼容所需 API，填写模型 ID、API 地址和 API Key 即可。安装阶段会对三类必填端点分别做 smoke test，不可用会要求重新填写。API Key 优先存入系统 keyring，不明文落盘。

### 产品级安装（推荐）

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
python3 mnemos_cli.py setup --dry-run --json   # 先看安装计划
python3 mnemos_cli.py setup                    # 交互式安装
```

`mnemos setup` 是推荐主入口，它把配置、Vault 初始化、Agent 接入、scheduler、部署验证串到同一个安装状态机里，会自动完成：

1. 检查 Python >= 3.10，安装依赖
2. 检测 Obsidian 并确认 Mnemos/raw 两个默认 Vault 路径
3. 生成 `~/.mnemos/configs/main.json`（权限 0600）
4. 初始化标准 Wiki 目录结构
5. 安装 AI Agent 接入（adapter hooks + MCP 配置）
6. 启动后台守护进程，配置系统定时任务（macOS launchd / Linux cron / Windows Task Scheduler）
7. 运行部署验证：三类必填模型端点 smoke test + 只读 E2E 探针

全自动模式（无交互，macOS / Linux）：

```bash
export MNEMOS_LLM_MODEL=your_llm_model_id
export MNEMOS_LLM_BASE_URL=https://your-llm-api.example/v1
export MNEMOS_LLM_API_KEY=your_llm_key
export MNEMOS_EMBEDDING_MODEL=your_embedding_model_id
export MNEMOS_EMBEDDING_BASE_URL=https://your-embedding-api.example/v1
export MNEMOS_EMBEDDING_API_KEY=your_embedding_key
export MNEMOS_RERANKER_MODEL=your_reranker_model_id
export MNEMOS_RERANKER_BASE_URL=https://your-reranker-api.example/v1
export MNEMOS_RERANKER_API_KEY=your_reranker_key
python3 mnemos_cli.py setup --yes
```

`--yes` 不会交互询问；三类必填端点任一缺失或 smoke test 失败都会直接退出。Windows 使用 `setup.bat` 或在 PowerShell 中等价设置环境变量。

升级、修复与卸载统一走同一状态机：

```bash
python3 mnemos_cli.py upgrade plan --json
python3 mnemos_cli.py upgrade apply --json      # 先自动创建全局快照
python3 mnemos_cli.py doctor repair-all --json
python3 mnemos_cli.py uninstall --preserve-data --json
```

`uninstall` 默认保留数据；真正删除数据必须先冻结、提供快照引用并二次确认。

### 手动安装

```bash
# 1. 克隆并安装
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
pip install -e .

# 2. 复制并编辑配置
mkdir -p ~/.mnemos/configs
cp config/config.example.json ~/.mnemos/configs/main.json
# 编辑 main.json：wiki 路径 + llm/embedding/reranker 的 base_url、model、api_key_source

# 3. 诊断与验证
python3 mnemos_cli.py doctor
python3 mnemos_cli.py setup --dry-run --json
python3 scripts/verify_installation.py --api-smoke
```

### 构建语义搜索索引（可选增强）

Embedding/Reranker 配置通过 smoke test 后，可构建向量索引提升未知查询的召回质量：

```bash
pip install -e ".[ml]"                          # 安装 hnswlib 等可选依赖
python3 scripts/build_embedding_index.py        # 构建索引
```

未安装时自动回退到内存索引，不影响功能。

### 命令行工具

```bash
mnemos setup                       # 安装/配置/验证入口
mnemos init                        # 交互式配置向导
mnemos doctor                      # 系统诊断（repair 子命令可修复）
mnemos status                      # 系统状态总览
mnemos health --json               # 机器可读健康检查（30 项检查）
mnemos config                      # 查看/编辑配置

# Agent 管理
mnemos agent list                  # 列出本机可用的 AI Agent
mnemos agent install               # 安装 adapter hooks + MCP 接入
mnemos agent doctor                # 诊断 Agent 状态

# 后台服务
mnemos daemon start|stop|status    # 守护进程管理
mnemos scheduler install-windows   # 注册 Windows 开机启动

# 管线
mnemos sync status                 # 同步状态
mnemos distill status              # 蒸馏队列状态
mnemos import <path>               # 导入文档（PDF/Word/PPT/Excel/HTML/EBOOK）
mnemos search <query>              # 上下文感知搜索
mnemos wiki read <page>            # 读取 Wiki 页面

# 认知层
mnemos observe run                 # 运行 Observation Engine（L3）
mnemos reflect manual              # 手动触发 Reflection（L4）
mnemos feedback stats              # 反馈闭环统计（L5）
mnemos persona behavior-metrics    # 画像消费效果指标
mnemos recap list                  # 复盘队列

# 评分与治理
mnemos scorer status               # 评分器状态和模式
mnemos kg doctor                   # 知识图谱诊断
mnemos dispute list                # 争议仲裁列表
mnemos blindspot list              # 知识盲区
mnemos data inventory --json       # 数据所有权清单
mnemos backup create               # 全局快照备份
```

顶层命令共 57 个，未列出的多为高级/调试/实验性功能，使用 `python3 mnemos_cli.py <command> --help` 查看具体参数。

## 与 AI Agent 集成

### 方式一：MCP 协议（推荐，通用）

任何支持 MCP 的 AI Agent 都可以接入。MCP server 注册 **57 个工具**，按功能分 5 组：

**core — 高频闭环**

| 工具 | 用途 |
|------|------|
| `preflight_inject` | 任务前装载历史经验（KIA 闭环第一步） |
| `guard_check` | 执行中风险守护，含分析循环/重复读取告警（KIA 闭环第二步） |
| `wiki_search` | 搜索知识库 |
| `wiki_read` | 读取指定页面 |
| `document_process` | 处理用户指定路径文档，进入蒸馏链路 |

**lifecycle — 会话捕获**

| 工具 | 用途 |
|------|------|
| `capture_turn` | 逐轮上报对话（< 200ms 入队） |
| `capture_session` | 批量上报整个 session |
| `end_session` | 标记 session 结束 |
| `capture_status` | 查询捕获队列状态 |

**extended — 知识与复盘**

| 工具 | 用途 |
|------|------|
| `knowledge_ingest` | 用户主动投喂知识（"记住这个"） |
| `knowledge_distill` | 触发知识蒸馏 |
| `wiki_build` / `wiki_write` | 触发 Wiki 构建 / 写入页面 |
| `memory_write_project` / `memory_write_framework` / `memory_write_global` | 按范围写入记忆 |
| `memory_search` | 按 project/framework/global 范围搜索记忆 |
| `session_search` | 搜索历史会话 |
| `check_pending_recaps` | 检查待复盘事项 |
| `recap_start` / `recap_submit` / `recap_finalize` / `recap_skip` / `recap_feedback` / `recap_status` / `recap_claim_owner` | 结构化三问复盘全流程 |
| `retrospective_list` | 列出可用的复盘经验 |
| `persona_summary` / `persona_update` / `persona_behavior_prompt` / `persona_behavior_metrics` / `persona_record_explicit_evidence` | 画像查询、更新与证据记录 |

**auxiliary — 系统与搜索**

| 工具 | 用途 |
|------|------|
| `health_check` | 系统健康快照（与 CLI 同一检查集） |
| `self_diagnose` / `detect_sources` | 自诊断 / 数据源连接状态 |
| `configure_wiki` | 配置 Wiki 路径 |
| `context_aware_search` | 上下文感知搜索（画像加权 + 图谱召回） |
| `knowledge_source_list` | 知识来源分布统计 |
| `signal_collect` | 触发信号采集 |
| `build_cognitive_state` | 构建认知状态快照 |
| `agent_runtime_probe` | 宿主运行能力验收探针 |

**advanced — 决策与认知**

| 工具 | 用途 |
|------|------|
| `intent_route` / `intent_correct` | 意图路由与纠正 |
| `predictive_push` / `push_feedback` / `delivery_display_ack` | 预测推送与投递反馈闭环 |
| `blindspot_check` | 盲区检测 |
| `freshness_check` | 知识新鲜度检查 |
| `observation_run` / `observation_search` | Observation Engine（L3） |
| `reflect_on_input` / `reflect_manually` / `reflection_feedback` / `reflection_pending` | Reflection（L4）与反馈（L5） |
| `record_decision` / `apply_outcome` | 决策记录与结果回填 |
| `wiki_write` | 受控写入 Wiki 页面 |

配置示例：

```json
{
  "mnemos": {
    "command": "mnemos",
    "args": ["mcp", "serve"]
  }
}
```

安全模型：`mnemos agent install` 为每个宿主签发独立的高熵 launch capability（只存 keyring 引用，不明文落盘）；每次 tool call 前重新验证撤销/过期状态。跨 Agent/project 能力由你显式授权（`mnemos agent grant-mcp`），调用方不能自报身份或扩权。

### 方式二：Adapter Hooks（Claude Code / Kimi / Crush）

运行 `mnemos setup` 或 `mnemos agent install` 时自动安装 hooks（Claude Code 写入 `~/.claude/settings.json`）。安装异常时运行 `mnemos doctor repair` 修复。

### 方式三：MCP-only 接入（Codex / Hermes / Kiro / OpenCode / OpenClaw）

这 5 个 Agent 通过 JSON MCP 配置接入，无需 hooks；`mnemos setup` 会自动写入它们的 MCP 配置与主动策略。

### 被动采集来源（Aider / Gemini CLI / Cursor / Windsurf）

这 4 个工具不支持主动接入，但 daemon 的 `raw_sync` 服务会周期解析它们的本地聊天记录文件，同样进入采集 → 蒸馏链路。

各 Agent 的详细接入文档见 `docs/integrations/` 目录。

## 与 Obsidian 的关系

Mnemos 与 [Obsidian](https://obsidian.md) 是互补关系，不是替代。

- Mnemos 的知识库层是**纯 Markdown + YAML Frontmatter**，不绑定任何特定工具
- 部署阶段要求安装 Obsidian，因为：
  1. **原生兼容**：Obsidian 的笔记格式就是 Markdown，无需导出/转换
  2. **双向链接**：`[[页面名]]` 语法自动构建知识图谱
  3. **图谱视图**：Obsidian 的 Graph View 就是知识图谱可视化
  4. **社区生态**：Dataview、Templater 等插件可与 Mnemos 的数据联动
  5. **本地优先**：和 Mnemos 的数据隐私策略一致，所有知识库内容存本地
- 配合方式：Obsidian 负责**知识的组织、可视化、人工编辑**；Mnemos 负责**知识的自动采集、raw 投影、蒸馏、评分、画像驱动、闭环进化**。人管创作，AI 管运营。
- 双 Vault 设计：raw Vault（默认 `~/Documents/raw`）保存各 Agent 的原始对话投影，是 `raw_events.db` 的可读展示层；Mnemos Vault（默认 `~/Documents/mnemos`）保存蒸馏后的认知库

### 数据所有权

- 所有数据存储在你的本地磁盘：Wiki/raw 是纯 Markdown，运行状态是本地 SQLite，不会上传到任何服务器
- `mnemos data inventory --json` 列出所有数据的保存位置、记录数、消费者和导出/冻结/删除策略
- `mnemos data export` 生成脱敏导出 manifest；`delete` 必须先冻结、提供快照引用并确认
- Mnemos 不会收集、上传或分享你的任何数据

## 配置

运行时权威配置文件位于 `~/.mnemos/configs/main.json`（跨平台统一路径，旧版 YAML 会自动迁移）。

配置优先级：**代码默认值 < JSON 配置文件 < 环境变量**（环境变量优先级最高）。

支持的主要环境变量：

| 环境变量 | 对应配置项 | 说明 |
|----------|-----------|------|
| `MNEMOS_DIR` | — | Mnemos 数据根目录（默认 `~/.mnemos`） |
| `MNEMOS_WIKI_DIR` / `WIKI_DIR` | `wiki.vault_path` | Wiki 知识库目录 |
| `MNEMOS_LLM_API_KEY` / `MNEMOS_LLM_BASE_URL` / `MNEMOS_LLM_MODEL` | `llm.*` | LLM（对话/蒸馏模型）端点 |
| `MNEMOS_EMBEDDING_API_KEY` / `MNEMOS_EMBEDDING_BASE_URL` / `MNEMOS_EMBEDDING_MODEL` | `embedding.*` | Embedding（向量/语义召回）端点 |
| `MNEMOS_RERANKER_API_KEY` / `MNEMOS_RERANKER_BASE_URL` / `MNEMOS_RERANKER_MODEL` | `reranker.*` | Reranker（搜索重排）端点 |
| `MNEMOS_MULTIMODAL_API_KEY` / `MNEMOS_MULTIMODAL_BASE_URL` / `MNEMOS_MULTIMODAL_MODEL` | `multimodal.*` | 多模态（图片解析）端点，可选 |

关键配置项示例：

```json
{
  "wiki": {
    "vault_path": "~/Documents/mnemos"
  },
  "llm": {
    "provider": "openai-compatible",
    "base_url": "https://your-llm-api.example/v1",
    "api_key_source": "env:MNEMOS_LLM_API_KEY",
    "model": "your-llm-model-id"
  },
  "embedding": {
    "enabled": true,
    "base_url": "https://your-embedding-api.example/v1",
    "api_key_source": "env:MNEMOS_EMBEDDING_API_KEY",
    "model": "your-embedding-model-id",
    "use_rerank": true
  },
  "reranker": {
    "enabled": true,
    "base_url": "https://your-reranker-api.example/v1",
    "api_key_source": "env:MNEMOS_RERANKER_API_KEY",
    "model": "your-reranker-model-id"
  },
  "trusted_push": {
    "mode": "off"
  },
  "delivery": {
    "preference": "balanced"
  }
}
```

- **API Key 管理**：`api_key_source` 优先使用 `keyring:REF`（系统 keyring），无法使用时才用 `env:VAR` 并显式接受降级；同一端点可配置 `api_key_sources` 多 Key 轮转（429/5xx 自动冷却）
- **投递偏好**：`delivery.preference` 可选 `quiet` / `balanced`（默认）/ `active`，控制主动推送的频率和冷却
- **可信推送**：`trusted_push.mode` 可选 `off`（默认）/ `shadow` / `enforce`

## 数据源与隐私

用户画像的数据源完全由你自选，默认只开启 AI 对话采集，其余需主动开启：

| 数据源 | 用途 | 隐私级别 |
|--------|------|---------|
| AI 对话 | 推断专注深度、质疑倾向、完美偏好 | 仅本地存储 |
| Git 提交 | 推断续航模式、创新倾向 | 仅统计信息，不存代码 |
| Wiki 交互 | 推断关注领域、学习路径 | 仅页面路径和动作类型 |
| 文件系统 | 推断活跃项目、节奏 | 仅本地处理，不上传 |

画像信号带有 scope/context（工作/个人/学习情境隔离）；每条画像断言保留隐私等级、过期时间、支持/反驳证据和修订策略。画像策略注入可通过 `persona.strategy_injection_enabled=false` 完全关闭。

## 技术栈

- **语言**：Python 3.10+
- **存储**：Markdown 文件（知识库）+ SQLite（约 20 个本地库：raw 事件/画像/评分/图谱/调度/账本）
- **协议**：MCP (Model Context Protocol) 用于 AI Agent 集成
- **蒸馏执行**：Mnemos 直接调用 LLM API（LLMApiChain 有序 failover，厂商无关）
- **评分算法**：ComplementNB + TfidfVectorizer + 贝叶斯后验更新
- **向量索引**：hnswlib（可选，`.[ml]` extra；不可用时回退内存索引）
- **调度**：拓扑排序 + ThreadPoolExecutor 并行执行
- **文档处理**：PDF / PPT / Excel / Word / HTML / EBOOK 解析
- **密钥管理**：系统 keyring 优先，env 引用为显式降级
- **核心依赖**：requests、pyyaml、jsonschema、watchdog、numpy、openai、anthropic、keyring、pypdf、python-docx、openpyxl、python-pptx、pdfplumber、beautifulsoup4、markdownify、ebooklib、psutil

## 项目状态

**Mnemos v2.0.0** — 核心链路可用，高级能力持续优化中。

### 已可用

- [x] **同步框架**：SyncEngine 8 步流水线 + 12 个 Agent Source + append-only raw revision
- [x] **七层蒸馏流水线**：噪音过滤 → 价值预判 → LLM 判断 → 知识提取 → 自检 → 跨 Agent 关联 → 反馈循环
- [x] **知识图谱**：实体/关系管理 + 置信度治理 + 上下文感知查询
- [x] **评分闭环**：COLD/WARM/HOT 三阶段 + 5 域评分器 + 蒸馏评分器 + 漂移检测
- [x] **认知链**：Observation（L3）→ Reflection（L4）→ Feedback（L5）+ 跨层认知图
- [x] **可信推送闭环**：ProposalQueue → 审批 → append-only Journal → 受控写入（off/shadow/enforce）
- [x] **复盘消费闭环**：复盘结论真正落到检索/策略/画像/调度/评分，负反馈可撤销
- [x] **MCP 服务器**：57 个工具覆盖知识库/摄入/会话/KIA/画像/决策/认知/系统
- [x] **Agent Kit**：8 个宿主 Agent（Claude Code / Kimi / Crush / Codex / Hermes / Kiro / OpenCode / OpenClaw）+ 4 个被动采集来源
- [x] **文档处理**：PDF / PPT / Excel / Word / HTML / EBOOK 解析入库
- [x] **语义搜索**：厂商无关 Embedding/Reranker 端点 + 向量索引
- [x] **可选多模态**：配置后图片/截图自动解析入库

### 持续完善中

- [ ] **评分器冷启动**：需积累训练样本才能进入 WARM/HOT 模式，随使用自然成熟
- [ ] **主动推送精准度**：随反馈数据积累持续提升
- [ ] **Web Dashboard / 本地控制中心**：当前请使用 CLI、MCP 和配置文件
- [ ] **Obsidian 插件**：双向同步与内联查询

## 致谢

- [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) —— LLM Wiki 模式的提出者，Mnemos 的核心灵感来源
- [Obsidian](https://obsidian.md) —— 知识管理的标杆工具，Mnemos 推荐的知识库可视化方案

## 许可证

[MIT License](LICENSE)

---

**Mnemos**（/ˈnɛmɒs/）—— 希腊神话中的记忆女神，谟涅摩叙涅。不是只帮你记住，而是在可审计、可配置、可降级的边界内，让 AI 在合适的时候想起相关知识并辅助行动。
