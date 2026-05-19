# Mnemos v0.2.0 架构设计文档

> 基于实际代码的完整架构说明
>
> **注意**：本文档部分章节描述的是 v0.1.x 时代的旧架构（Ingest Engine / Guards / Pipeline 模型）。
> 当前 v0.2.0 架构已升级为三层模型（Agent 适配器层 → 事件总线 → 核心服务层），详见下文「系统架构概览」。
> 旧章节将在后续版本中逐步更新。

---

## 目录

1. [系统架构概览](#系统架构概览)
2. [核心数据流](#核心数据流)
3. [五大子系统详解](#五大子系统详解)
4. [数据模型](#数据模型)
5. [状态机设计](#状态机设计)
6. [扩展点设计](#扩展点设计)

---

## 系统架构概览

### 整体架构图（v0.2.0 三层模型）

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Agent 适配器层 (Olympus)                            │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────┐ │
│  │ Apollon │ │Caduceus │ │ Typhon  │ │  Musae  │ │Daedalus│ │
│  │(Claude) │ │(Hermes) │ │(OpenClw)│ │(OpenCod)│ │(Codex) │ │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └───┬────┘ │
│       │           │           │           │           │      │
│       └───────────┴───────────┴───────────┴───────────┘      │
│                           │                                  │
│                           ▼                                  │
├──────────────────────────────────────────────────────────────┤
│  Layer 2: 统一事件总线 (Mnemos Event Bus)                     │
│  ~/.mnemos/events/  —  文件系统事件队列（跨进程/跨 Agent）   │
│  session.start | session.end | distill.request | signal.batch │
├──────────────────────────────────────────────────────────────┤
│  Layer 1: Mnemos 核心服务（Agent-Agnostic）                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐  │
│  │Hephaestus│ │   KIA    │ │ Persona  │ │   Daemon       │  │
│  │(蒸馏Worker)│ │(知识注入) │ │(画像系统) │ │ (后台服务)      │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └───────┬────────┘  │
│       │            │            │               │           │
│       └────────────┴────────────┴───────────────┘           │
│                           │                                  │
│                           ▼                                  │
│  ┌────────────────────────────────────────────────────────┐ │
│  │         Wiki 知识库 (Obsidian Vault)                    │ │
│  │  00-Inbox/  01-Projects/  02-Areas/  03-Resources/      │ │
│  │  04-Archives/  05-Periodic/  06-Memos/  07-Shadow/     │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 各层职责

**Layer 3 — Agent 适配器层 (Olympus)**
- `integrations/olympus.py` — `AgentAdapter` 基类 + `AgentRegistry` 注册中心
- `integrations/apollon.py` — Claude Code 适配器（Hooks、settings.json）
- `integrations/caduceus.py` — Hermes 适配器（Poll、Inbox 轮询）
- `integrations/typhon.py` — OpenClaw 适配器（SQLite、Hooks）
- `integrations/musae.py` — OpenCode 适配器（JSON Config、Hooks）
- `integrations/daedalus.py` — Codex 适配器（File-based、Windows .bat）
- 所有适配器实现统一的 `AgentAdapter` 接口：`name`, `priority`, `is_available()`, `get_data_dir()`, `on_session_start()`, `on_session_end()`, `collect_signals()`, `install_hooks()`, `delegate_distillation()`

**Layer 2 — 统一事件总线**
- `core/mnemos_bus.py` — 文件系统事件队列
- 事件目录：`~/.mnemos/events/{inbox,processing,archive}/`
- 标准事件：`session.start`, `session.end`, `distill.request`, `signal.batch`
- 提供 `publish()`, `poll()`, `ack()` 接口
- 跨进程、跨 Agent，无需额外消息队列依赖

**Layer 1 — Mnemos 核心服务**
- `core/hephaestus_worker.py` — 蒸馏 Worker（轮询队列 → 委托 Agent → 收集结果 → 验证格式 → 移入 Inbox）
- `core/kia/` — Knowledge-in-Action 闭环（预加载、守护、复盘）
- `core/persona/` — 用户画像系统（信号采集 → 画像分析 → 盲区检测 → 校准）
- `mnemos_daemon.py` — 后台守护进程（信号采集、蒸馏调度、画像更新、知识调度）

### 旧架构图（v0.1.x，仅供参考）

```
┌─────────────────────────────────────────────────────────────────────┐
│                         输入层 (Input Layer)                        │
├─────────────────────────────────────────────────────────────────────┤
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐            │
│  │  Claude  │  │  Hermes  │  │ OpenClaw │  │  Manual  │            │
│  │  Hooks   │  │  Poll    │  │  SQLite  │  │  Input   │            │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘            │
│       └─────────────┴─────────────┴─────────────┘                    │
│                           │                                         │
│                           ▼                                         │
├─────────────────────────────────────────────────────────────────────┤
│                    处理层 (Processing Layer)                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    【Ingest Engine】                         │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │   │
│  │  │   Queue     │  │   Batch     │  │   Retry     │         │   │
│  │  │  (Serial)   │  │  (Buffer)   │  │  (3 times)  │         │   │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘         │   │
│  │         └─────────────────┼─────────────────┘               │   │
│  │                           ▼                                  │   │
│  │  ┌──────────────────────────────────────────────────────┐   │   │
│  │  │                   防护层 (Guards)                     │   │   │
│  │  │  L0: 内容去重 → L3: 引用污染 → L4: 上下文回忆 → LQ: 质量 │   │   │
│  │  └──────────────────────────────────────────────────────┘   │   │
│  │                           │                                  │   │
│  │                           ▼                                  │   │
│  │  ┌──────────────────────────────────────────────────────┐   │   │
│  │  │                  处理管道 (Pipeline)                  │   │   │
│  │  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │   │   │
│  │  │  │ 4-Cat    │ │ Quality  │ │ AI Self  │ │ Multi    │ │   │   │
│  │  │  │ Engine   │→│ Check    │→│ Check    │→│ Source   │ │   │   │
│  │  │  │          │ │          │ │ (3-pass) │ │ Validate │ │   │   │
│  │  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘ │   │   │
│  │  └──────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                               │                                     │
│                               ▼                                     │
├─────────────────────────────────────────────────────────────────────┤
│                      存储层 (Storage Layer)                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    【Wiki Structure】                        │   │
│  │                                                              │   │
│  │   Sources/        ← 原始来源记录                             │   │
│  │   ├── {date}_{source}_{id}.md                               │   │
│  │                                                              │   │
│  │   Entities/       ← 实体页面                                │   │
│  │   ├── {entity_name}.md                                      │   │
│  │   └── _index.md     (实体索引)                              │   │
│  │                                                              │   │
│  │   Concepts/       ← 概念页面                                │   │
│  │   ├── {concept_name}.md                                     │   │
│  │   └── _index.md     (概念索引)                              │   │
│  │                                                              │   │
│  │   Synthesis/      ← 合成页面                                │   │
│  │   └── {cluster}_synthesis.md                                │   │
│  │                                                              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                               │                                     │
│                               ▼                                     │
├─────────────────────────────────────────────────────────────────────┤
│                      服务层 (Service Layer)                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │   Expand     │  │    Heat      │  │    AI        │              │
│  │   Engine     │  │   Tracker    │  │   Search     │              │
│  │   (L3+)      │  │  (L0-L9)     │  │ (Heat Ctrl)  │              │
│  └──────────────┘  └──────────────┘  └──────────────┘              │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 核心数据流

### Ingest流程

```
L1原始记录
    │
    ▼
┌─────────────────────┐
│ 1. 内容去重检查     │ ← Guard L0
│    (content_hash)   │
└──────────┬──────────┘
           │ 重复 → 跳过
           ▼
┌─────────────────────┐
│ 2. Wiki污染检测     │ ← Guard L3
│    (wiki_ref密度)   │
└──────────┬──────────┘
           │ 污染 → 仅创建Source
           ▼
┌─────────────────────┐
│ 3. 上下文回忆检测   │ ← Guard L4
│    (context:recall) │
└──────────┬──────────┘
           │ 回忆内容 → 仅创建Source
           ▼
┌─────────────────────┐
│ 4. 质量评估         │ ← Guard LQ
│    (5维评分)        │
└──────────┬──────────┘
           │ <40分 → 隔离
           ▼
┌─────────────────────┐
│ 5. 四大类识别       │
│    (分类+提炼)      │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 6. AI三道自检       │
│    (唯一/严谨/中立) │
└──────────┬──────────┘
           │ 失败 → 拒绝
           ▼
┌─────────────────────┐
│ 7. 多源验证         │
│    (四级验证)       │
└──────────┬──────────┘
           │ 冲突 → 隔离
           ▼
┌─────────────────────┐
│ 8. 写入Wiki         │
│    (Source+Entities)│
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 9. 热力初始化       │
│    (L1, 0分)        │
└─────────────────────┘
```

### AI搜索流程

```
AI查询
    │
    ▼
┌─────────────────────┐
│ 1. 语义搜索         │
│    (关键词+向量)    │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 2. 热力过滤         │
│    (排除L0沉睡)     │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 3. 排序             │
│    (热力+相关度)    │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 4. 读取深度控制     │
│    (按等级裁剪)     │
├─────────────────────┤
│ L0: 仅元数据        │
│ L1-L3: 摘要100字    │
│ L4-L6: 段落500字    │
│ L7-L9: 全文         │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 5. 热力加成         │
│    (+3/+8/+...)     │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 6. 返回结果         │
└─────────────────────┘
```

---

## 五大子系统详解

### 1. 四大类信息识别引擎

**核心类**: `FourCategoryEngine`

```python
class FourCategoryEngine:
    def classify_and_refine(self, content: str, tags: List[str]) -> RefinedContent:
        # 1. 三层分类
        classification = self._three_layer_classify(content, tags)
        # 2. 差异化提炼
        cleaned = self._apply_strategy(content, classification)
        # 3. 实体提取
        entities = self._extract_entities(cleaned, classification.type)
        return RefinedContent(
            content_type=classification.type,
            cleaned_content=cleaned,
            entities=entities
        )
```

**三层分类器**:

```
输入: content + tags
    │
    ├──→ Layer 1: 元数据识别 (权重0.3)
    │           基于标签快速判断
    │           source=claude → code (0.9)
    │
    ├──→ Layer 2: 结构特征 (权重0.5)
    │           正则匹配代码/业务/知识/感悟模式
    │           代码块存在 → code (0.3)
    │           金额出现 → business (0.3)
    │
    └──→ Layer 3: 语义识别 (权重0.2)
                关键词密度统计
                关键词超过阈值 → 对应类型
```

### 2. 质量评估体系

**核心类**: `ContentQualityAssessor`

**五维评分算法**:

```python
def assess(self, content: str) -> QualityScore:
    # 各维度评分 (0-1)
    density = self._assess_density(content)      # 信息密度
    structure = self._assess_structure(content)  # 结构化
    uniqueness = self._assess_uniqueness(content) # 独特性
    practicality = self._assess_practicality(content) # 实用性
    citation = self._assess_citations(content)   # 引用质量

    # 加权计算 (0-100)
    total = (
        density * 0.20 +
        structure * 0.20 +
        uniqueness * 0.20 +
        practicality * 0.25 +
        citation * 0.15
    ) * 100

    # 惩罚项
    penalties = self._calculate_penalties(content)
    final_score = max(0, total - sum(p.deduction for p in penalties))

    return QualityScore(total=final_score, ...)
```

**密度评估指标**:
- 有效词比例 = 去除停用词后 / 总词数
- 词汇多样性 = 唯一词数 / 总词数
- 实体密度 = 实体标记数 / 100词

### 3. 多源验证机制

**核心类**: `CrossValidator`, `TieredContentFilter`

**四级验证流程**:

```
内容文本
    │
    ▼
┌─────────────────────┐
│ StatementClassifier │
│  - 分句             │
│  - 句式匹配         │
│  - 类型标记         │
└──────────┬──────────┘
           │
           ▼  [fact, description, definition, conclusion, evaluation, prediction]
┌─────────────────────┐
│ CrossValidator      │
│  - 提取硬事实       │
│  - 两两比较         │
│  - 冲突检测         │
└──────────┬──────────┘
           │
           ▼  验证状态: pending/cross_checking/verified/core/conflicted
┌─────────────────────┐
│ TieredContentFilter │
│  - 按级别过滤       │
│  - 拦截低级别表述   │
│  - 存入隔离库       │
└──────────┬──────────┘
           │
           ▼
    [allowed_statements] [blocked_statements→quarantine]
```

**硬事实冲突检测**:

```python
def _check_hard_fact_conflict(self, facts1, facts2):
    conflicts = []
    hard_facts = [f for f in facts if f.priority == FactPriority.HARD]

    for f1 in hard_facts1:
        for f2 in hard_facts2:
            if f1.type == f2.type and f1.value != f2.value:
                # 同类硬事实值不同 = 冲突
                conflicts.extend([f1, f2])

    return conflicts  # 有冲突 → 状态降级为 conflicted
```

### 4. Expand 2.0 引擎

**核心类**: `ExpandEngineV2`, `ExpandExecutor`

**动态阈值计算**:

```python
class DynamicThresholdCalculator:
    def calculate(self, entity_type: str, source_count: int) -> Threshold:
        base = self.thresholds[entity_type]

        # 素材越多，阈值越高
        source_bonus = min(source_count * 5, 20)

        # 热力越高，要求越低
        heat_discount = self._heat_discount(heat_score)

        return Threshold(
            source_count=base.min_sources,
            heat_score=base.min_heat - heat_discount + source_bonus
        )
```

**三级Expand**:

| 级别 | 触发条件 | 扩展策略 | 输出 |
|------|----------|----------|------|
| L1 | 同义词检测 | 名称变体、缩写 | 实体别名 |
| L2 | 关系挖掘 | 共现实体、引用关系 | 关系图谱 |
| L3 | 深度合成 | 多源知识合并 | 合成页面 |

### 5. 热力追踪系统

**核心类**: `WikiHeatTracker`

**10级状态机**:

```
                    ┌─────────────────────────────────────┐
                    │                                     │
    ┌───────────┐   │   ┌───┐   ┌───┐   ┌───┐          │
    │   Input   │───┴──→│L0 │──→│L1 │──→│L2 │──→ ...   │
    └───────────┘       └───┘   └───┘   └───┘          │
     (负分/衰减)        (-100~0) (0-20) (20-50)        │
                          ▲                            │
                          │ 唤醒 +30分                 │
                          └────────────────────────────┘
```

**每日上限控制**:

```python
def _add_heat(self, page_id: str, points: float):
    today_added = self._get_today_added(page_id)

    if today_added >= DAILY_CAP:  # 50分上限
        return {"blocked": True, "reason": "daily_cap"}

    actual_points = min(points, DAILY_CAP - today_added)
    new_score = min(500, current_score + actual_points)  # L9封顶

    # 检查升级
    if new_level > old_level:
        self._on_level_up(page_id, old_level, new_level)
```

**升级回调**:
- L3+: 标记Expand资格
- L5+: 标记合成候选
- L7+: 加入AI优先读取
- L9: 最高优先级

---

## 数据模型

### 核心实体关系

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Source    │────→│   Entity    │←────│   Concept   │
│   (L1)      │     │   (核心)    │     │   (类型)    │
└─────────────┘     └──────┬──────┘     └─────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌─────────┐  ┌─────────┐  ┌─────────┐
        │  Heat   │  │ Expand  │  │Synthesis│
        │  (L0-L9)│  │ (L3+)   │  │ (L5+)   │
        └─────────┘  └─────────┘  └─────────┘
```

### 数据库表结构

**wiki_heat** (热力主表)
```sql
CREATE TABLE wiki_heat (
    page_id TEXT PRIMARY KEY,
    current_level TEXT DEFAULT 'L1',
    heat_score REAL DEFAULT 0,
    ai_search_hits INTEGER DEFAULT 0,
    ai_citation_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    consecutive_search_days INTEGER DEFAULT 0,
    today_heat_added REAL DEFAULT 0,
    last_heat_date TEXT,
    status TEXT DEFAULT 'active'  -- active/sleeping
);
```

**quarantined_statements** (隔离库)
```sql
CREATE TABLE quarantined_statements (
    id INTEGER PRIMARY KEY,
    entity_name TEXT NOT NULL,
    statement_text TEXT,
    statement_type TEXT,
    required_level INTEGER,
    current_sources INTEGER,
    reviewed BOOLEAN DEFAULT 0,
    decision TEXT  -- approved/rejected/auto_released
);
```

**quality_scores** (质量评分)
```sql
CREATE TABLE quality_scores (
    id INTEGER PRIMARY KEY,
    page_id TEXT NOT NULL,
    total_score REAL,
    density_score REAL,
    structure_score REAL,
    uniqueness_score REAL,
    practicality_score REAL,
    citation_score REAL,
    quality_level TEXT
);
```

---

## 状态机设计

### Ingest任务状态机

```
                    ┌─────────┐
        ┌──────────→│ PENDING │←────────────────┐
        │           └────┬────┘                 │
        │                │ submit               │
        │                ▼                      │
        │           ┌─────────┐    fail(max)   │
        │     ┌────→│PROCESSING│───────────────┼──→ FAILED
        │     │     └────┬────┘               │
        │   retry      success                │
        │     │        /                      │
        │     │       /                       │
        │     │      ▼                        │
        │  ┌────────┐/                    ┌───┴───┐
        └─←│RETRYING│────────────────────→│COMPLETED│
           └────────┘                     └───────┘
```

### 实体验证状态机

```
         ┌─────────────┐
    ┌───→│   pending   │←── 初始状态
    │    └──────┬──────┘
    │           │ 多源交叉验证
    │           ▼
    │    ┌─────────────┐
    └───←│cross_checking│←── 2-3源
    │    └──────┬──────┘
    │           │ 一致性>0.7
    │           ▼
    │    ┌─────────────┐
    └───←│   verified  │←── 4-5源
    │    └──────┬──────┘
    │           │ 一致性>0.8
    │           ▼
    │    ┌─────────────┐
    └───←│    core     │←── 6+源
    │    └──────┬──────┘
    │           │
    │    ┌──────┴──────┐
    └────┤  conflicted │←── 硬事实冲突
         └─────────────┘
```

---

## 扩展点设计

### 插件接口

```python
# 1. 自定义分类器
class CustomClassifier(ContentClassifier):
    def _layer2_structure(self, content: str):
        # 添加自定义模式
        patterns = [
            (r'custom_pattern', 0.3, 'custom_feature')
        ]
        return super()._layer2_structure(content, patterns)

# 2. 自定义质量维度
class CustomQualityAssessor(ContentQualityAssessor):
    def _assess_custom(self, content: str) -> float:
        # 自定义评估逻辑
        score = custom_logic(content)
        return score

# 3. 自定义热力规则
class CustomHeatTracker(WikiHeatTracker):
    def _calculate_level(self, score: float) -> str:
        # 自定义等级划分
        if score > custom_threshold:
            return "L10"  # 添加新等级
        return super()._calculate_level(score)
```

### 配置扩展

```yaml
# config/custom_rules.yaml
custom_classifiers:
  - name: medical
    patterns:
      - r'\b(diagnosis|treatment|symptom)\b'
    type: knowledge

custom_quality_rules:
  - name: citation_count
    weight: 0.1
    min_citations: 3

custom_heat_rules:
  - event: external_share
    points: 15
    daily_cap: 30
```

---

## 性能指标

| 指标 | 目标值 | 实际值 | 状态 |
|------|--------|--------|------|
| Ingest吞吐量 | 100条/小时 | 实测 | ✅ |
| 搜索响应时间 | <100ms | 实测 | ✅ |
| 热力计算延迟 | <10ms | 实测 | ✅ |
| 质量评估时间 | <50ms | 实测 | ✅ |
| 数据库大小 | <1GB | 实测 | ✅ |

---

## 部署架构

```
┌─────────────────────────────────────┐
│           User Machine              │
│  ┌─────────────────────────────┐   │
│  │   Mnemos System         │   │
│  │  ┌─────────┐  ┌─────────┐  │   │
│  │  │Ingest   │  │Scheduler│  │   │
│  │  │Engine   │  │(launchd)│  │   │
│  │  └────┬────┘  └─────────┘  │   │
│  │       │                     │   │
│  │  ┌────┴─────────────────┐  │   │
│  │  │   SQLite Databases   │  │   │
│  │  │  (local storage)     │  │   │
│  │  └─────────────────────┘  │   │
│  └─────────────────────────────┘   │
│            │                        │
│            ▼                        │
│  ┌─────────────────────────────┐   │
│  │   Wiki Directory            │   │
│  │   (Markdown files)          │   │
│  └─────────────────────────────┘   │
│            │                        │
└────────────┼────────────────────────┘
             │
             ▼
┌─────────────────────────────────────┐
│        External Services            │
│  ┌─────────┐  ┌─────────────────┐  │
│  │  Memos  │  │  Claude/OpenAI  │  │
│  │  (API)  │  │  (Optional)     │  │
│  └─────────┘  └─────────────────┘  │
└─────────────────────────────────────┘
```

---

**文档版本**: v4.0.0 | **代码版本**: 4.0.0 | **更新日期**: 2026-05-02
