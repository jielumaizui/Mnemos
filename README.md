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

## 架构概览

```
原始资料层 → 蒸馏层 → 知识库层 → 应用层 (KIA) → 进化层 → 画像层
```

```
┌──────────────────────────────────────────────────────────────┐
│                       输入层 (Input Layer)                    │
│   Claude Hooks · Hermes Poll · OpenClaw SQLite · Manual     │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                     处理层 (Processing Layer)                 │
│  ┌─────────┐   ┌───────────────────────────────────────┐    │
│  │  Ingest  │ → │  Guards: L0去重→L3污染→L4回忆→LQ质量  │    │
│  │  Engine  │   └───────────────┬───────────────────────┘    │
│  │  Queue   │                   ▼                           │
│  │  Batch   │   ┌───────────────────────────────────────┐    │
│  │  Retry   │ → │  Pipeline: 4-Cat→质量→AI三检→多源验证  │    │
│  └─────────┘   └───────────────────────────────────────┘    │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│              存储层 (Storage Layer) — 纯 Markdown            │
│   Sources/   Entities/   Concepts/   Synthesis/              │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    服务层 (Service Layer)                     │
│   Expand Engine (L3+) · Heat Tracker (L0-L9) · AI Search   │
└──────────────────────────────────────────────────────────────┘
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
cp config/config.example.yaml ~/.config/mnemos/config.yaml
# 编辑配置后
mnemos doctor
```

`mnemos doctor` 会自动检测系统状态，检查依赖是否就绪。

### 命令行工具

```bash
mnemos init          # 交互式配置向导
mnemos doctor        # 系统诊断
mnemos status        # 查看系统状态
mnemos config        # 查看/编辑配置
mnemos mcp serve     # 启动 MCP 服务器
```

## 与 AI Agent 集成

### 方式一：MCP 协议（推荐，通用）

任何支持 MCP 的 AI Agent 都可以接入：

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
- 你不需要 Memos 也能用 Mnemos（支持手动输入和其他数据源）
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

配置文件位于 `~/.config/mnemos/config.yaml`（Linux/macOS）或 `%APPDATA%\mnemos\config.yaml`（Windows）。

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
- **依赖**：requests、pyyaml（核心仅两个依赖）

## 项目状态

Mnemos 目前处于 **Alpha** 阶段（v0.1.0）。核心架构已稳定，但 API 可能在后续版本中调整。

- [x] 六层架构：采集→蒸馏→知识库→KIA→进化→画像
- [x] 五道 Guard 防护 + 三道 AI 自检
- [x] 热力评分 L0-L9
- [x] MCP 服务器
- [x] CLI 工具（init / doctor / status / config）
- [x] Claude Code Hooks 集成
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
