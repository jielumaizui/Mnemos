# core/kia — Knowledge-in-Action Engine

KIA（Knowledge-in-Action Engine）是 Mnemos 的**决策辅助层**：知识不是存完就完了，它应该在决策中活着。KIA 负责把已蒸馏的结构化知识注入回 Agent 工作流，并在任务前、任务中、任务后提供预加载、守护、提醒、复盘与推送。

> 命名来源：KIA 子系统大量借用希腊神话人物作为领域隐喻。为降低认知成本，本 README 同时列出“业务名 → 文件名 → 核心类”映射。

---

## 目录

- [设计目标](#设计目标)
- [模块地图](#模块地图)
- [关键流程](#关键流程)
- [对外接口](#对外接口)
- [测试入口](#测试入口)

---

## 设计目标

1. **知识要能用**：把 Wiki / 知识图谱里的内容，在 Agent 执行任务前主动推送。
2. **执行中要稳**：检测当前任务与历史经验的偏差，实时提醒或确认。
3. **任务后要复盘**：自动触发经验抽取、盲区检测、知识免疫系统扫描。
4. **跨 Agent 一致**：不同 Agent 采集的知识在 KIA 层统一消费。

---

## 模块地图

| 业务名 | 文件名 | 核心类 | 一句话职责 |
|---|---|---|---|
| 调度中心 | `chronos.py` | `KnowledgeScheduler` | KIA 16 步调度中心，事件/定时/条件/被动四类触发，拓扑排序并行执行。 |
| 预加载注入器 | `prophasis.py` | `Prophasis` | 任务前从 `06-Retrospectives/` 装载历史经验，决定直接注入、记入调度器或跳过。 |
| 执行守护 | `aegis.py` | `Aegis` | 任务执行中实时守护，三级策略（轻微/中等/严重偏差）选择记录、提醒或打断确认。 |
| 蒸馏队列 | `amphora.py` | `Amphora` | 双耳瓶蒸馏队列，按 source/session/input revision 和 generation 管理 pending/processing/done/failed/archived；入队与终态均返回 typed receipt。 |
| 知识图谱 | `knowledge_graph.py` | `KnowledgeGraph` | 基于 SQLite 的 Wiki 页面语义关系管理器，支持 CRUD、路径查找、冲突检测、导出。 |
| 实体管理 | `entity_manager.py` | `EntityManager` | 从 Wiki 提取实体，负责质量评分、贝叶斯更新、别名解析。 |
| 关系管理 | `relation_manager.py` | `RelationManager` | 提取关系、发现隐式关系、维护关系置信度。 |
| 跨 Agent 链接 | `cross_agent_linker.py` | `CrossAgentLinker` | 蒸馏生成新页面后，检测其他 Agent 的相似主题页面并添加双向链接。 |
| 知识 DNA | `genos.py` | `DNAEngine` | 去重/聚类/隐含关系推断/版本追踪的指纹系统，零外部依赖。 |
| 知识免疫 | `hygieia.py` | `KnowledgeImmuneSystem` | 自动检测冲突、过时、孤立、低置信度、重复、质量、循环依赖等七类问题；`mnemos immune scan --write-report` 可写出 Markdown 健康报告。 |
| 主动推送 | `teiresias.py` | `PredictivePush` | 基于上下文主动推送可能需要的知识。 |
| 认知决策飞轮 | `ixion.py`, `cognitive_decision_assets.py` | `CognitiveDecisionFlywheel`（`SkillWikiFlywheel` 为 legacy alias）, `CognitiveDecisionAsset` | 将反复出现的判断标准、失败边界和验证 recipe 沉淀为 `cognitive_decision_asset.v1`；automation skill 只允许作为已验证资产的派生产物。 |
| 影子页面 | `hecate.py` | `ShadowPage`, `PremiseValidator` | 联网搜索 Wiki 页面的外部相关信息，不污染主页面；前提变化验证器可输出 old→new 状态变化报告。 |
| 复盘/决策提取 | `metis.py` | `Metis` | 基于知识库生成个人知识画像与学习曲线报告；`knowledge_profile` 默认调度步骤调用 `ProfileGenerator.generate_and_report()`。 |
| 版本时间旅行 | `ananke.py` | — | 记录知识页面修改历史，支持快照、diff、回溯恢复。 |
| 归档摆渡 | `charon.py` | `ConnectWorker` | L2 → L3 关联层，将蒸馏结果摆渡到 Obsidian vault 的目录结构；`--watch` 支持 `--once`、`--max-cycles`、`--run-seconds`、`--interval`，`--dry-run` 只做抽取/分类预览，不写 KG 或触发 embedding API。 |
| 任务分类 | `dike.py` | `TaskClassifier` | 通用任务分类器，支持关键词、历史模式、LLM 语义确认；无显式目标时通过 `get_expected_goal_prompts()` 给分类结果补目标澄清提示。 |
| 对话提醒 | `dialog_reminder.py` | `DialogReminderQueue` | 对话界面多渠道、分层级提醒队列与页面横幅注入。 |
| 自动复盘 | `epimetheus.py` | `AutoRetrospective` | 检测任务结束或复盘关键词，自动生成结构化复盘报告。 |
| 时间解析 | `kairos.py` | `TimeParser` | 解析会话中的中英文相对时间与周期性信息。 |
| 知识演化 | `proteus.py` | `KnowledgeEvolutionEngine` | 知识版本迭代追踪与新鲜度检查。 |
| 熵减引擎 | `eris.py` | `EntropyEngine` | 检测冗余/相似内容并生成合并/删除/引用建议。 |
| 压力测试 | `stress_test.py` | `StressTestEngine` | 主动挑战知识边界条件，发现盲区并输出韧性评分。 |
| 可证伪性 | ShadowPage / 争议流程 | — | 完整逻辑已并入 ShadowPage / 争议流程；独立 `aporia.py` 已删除。 |
| 知识轨迹 | `ariadne.py` | `KnowledgeTrail` | 追踪知识查询、引用、修改、效果全生命周期轨迹。 |

其他辅助模块：

- `kia_event_consumer.py` — 消费 MnemosBus 事件并路由到 KIA 各子系统。
- `kg_event_handler.py` — 知识图谱变更事件处理。
- `policy.py` / `rule_scorer.py` — KIA 策略与规则评分；`mnemos policy list|commit|rollback` 提供 EffectivePolicy shadow 人工裁决入口。
- `module_registry.py` / `stress_test.py` / `eris.py` / `proteus.py` 等 — 模块注册、EventBus bridge、压力测试、混沌工程、动态适配。

---

## 关键流程

### 1. 任务前预加载（Prophasis → Preflight Inject）

```
用户开始新任务
    ↓
Preflight / Prophasis 检索相关复盘经验
    ↓
决策：直接注入 prompt / 延迟提醒 / 跳过
    ↓
宿主 Agent 收到上下文提示
```

### 2. 任务中守护（Aegis → Guard Check）

```
Agent 执行中
    ↓
Aegis 对比当前行为与历史经验
    ↓
轻微 → 记录日志
中等 → 自然语言提醒
严重 → 打断确认
```

### 3. 任务后蒸馏与免疫（Amphora → Hephaestus → Hygieia）

```
对话/文档结束
    ↓
Amphora 入队待蒸馏材料
    ↓
Hephaestus 蒸馏成 Wiki 页面
    ↓
CrossAgentLinker 建立跨 Agent 链接
    ↓
Hygieia 扫描质量问题
    ↓
Charon / 归档模块分类入 Obsidian vault
```

Amphora 不再使用 session-only unique 或进程内永久 dedupe：相同 input revision 幂等复用，内容变化产生新 generation。任务只有在 Hephaestus 返回 durable page 或 explicit intentional-skip terminal receipt 后才进入 `done`；proposal/partial/retry/write failure 继续保留为可恢复状态。历史无输出 `done` 与 Capture 无 handoff 可用 `python3 scripts/reconcile_pipeline_receipts.py` 只读发现，备份后再显式 `--apply`。

---

## 对外接口

KIA 的主要入口通常由 `mnemos_daemon.py` 中的 daemon 调度/服务循环调用：

- `Prophasis.preflight_inject(...)` — 任务前知识装载。
- `Aegis.guard_check(...)` — 执行中风险检查。
- `KnowledgeScheduler.run_cycle(...)` — 触发一次 KIA 调度循环。
- `Charon.run_connect_cycle(..., write_relations=False)` — daemon `wiki_route` 的轻量路由模式，只移动/标记 Inbox 页面，不写 KG cooccurrence 关系；完整 KG 关系构建应由手工 Charon connect 或显式重型调度触发。
- daemon 启动时必须先完成 KIA 模块注册，再启动 EventBus dispatch；否则 KIA wildcard 订阅未就绪时发布的真实事件会成为 no-consumer backlog。可丢弃 telemetry 事件可以归档，业务事件应进入 dead-letter 供审计，而不是长期停留在 pending/processing。
- `KnowledgeImmuneSystem.full_scan(...)` — 主动健康扫描；`generate_report_markdown(...)` 由 `mnemos immune scan --write-report` 渲染用户可读报告。
- `PredictivePush.push(...)` — 预测性知识推送。

MCP 工具（`integrations/agora.py`）也会暴露部分 KIA 能力给外部 Agent。

---

## 测试入口

```bash
# KIA 单元测试
pytest tests/unit/kia/
pytest tests/unit/test_chronos.py
pytest tests/unit/test_amphora.py
pytest tests/unit/test_knowledge_graph.py
pytest tests/unit/test_entity_manager.py
pytest tests/unit/test_relation_manager.py
pytest tests/unit/test_cross_agent_linker.py
pytest tests/unit/test_genos.py
pytest tests/unit/test_hygieia.py
pytest tests/unit/test_prophasis.py
pytest tests/unit/test_aegis.py

# KIA 集成测试
pytest tests/integration/test_kia_flywheel_persona.py
pytest tests/unit/test_chronos.py
```
