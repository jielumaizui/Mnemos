# CHANGELOG

> 单一事实源 — 所有历史修改、审计、集成报告统一归档于此文件。
> 更早的零散报告原文已合并至此后删除（2026-05-02）。

---

## [v0.2.0] — 五 Agent 全适配重构版（2026-05-19）

> 从 "Claude Code First" 到 "Agent-Agnostic" 的架构重构。所有 5 个 Agent 适配器成为一等公民，统一事件总线实现跨 Agent 通信。

### Added

- **五 Agent 适配器全部可用**
  - `integrations/apollon.py` — Claude Code（Hooks + settings.json）
  - `integrations/caduceus.py` — Hermes（Poll + Inbox 轮询）
  - `integrations/typhon.py` — OpenClaw（SQLite + Hooks）
  - `integrations/musae.py` — OpenCode（JSON Config + Hooks）
  - `integrations/daedalus.py` — Codex（File-based + Windows .bat wrapper）
- **统一事件总线** `core/mnemos_bus.py`
  - 文件系统事件队列（`~/.mnemos/events/{inbox,processing,archive}/`）
  - 标准事件格式：session.start / session.end / distill.request / signal.batch
  - 跨进程、跨 Agent 通信，无需额外依赖
- **统一蒸馏 Prompt** `core/hephaestus/distillation_prompts.py`
  - 单一 truth source，所有 Agent 使用完全相同的蒸馏 prompt
  - 支持数据蒸馏模式（Data Distillation Mode）
- **蒸馏格式验证层**
  - `HephaestusWorker._validate_distill_output()` 严格校验 JSON 格式
  - judgment 字段必须属于 {knowledge, skill, skip}
  - knowledge 判定要求 fragments 数组且每个 fragment 有 title + form
  - 无效输出自动移入 `distill_failed/`，避免污染 Inbox
- **Skip 智能过滤**
  - 判定为 skip 的蒸馏结果直接丢弃，不进入 Wiki Inbox
- **画像冷启动**
  - `PersonaStore._create_default_persona()` 为新用户生成默认模板
  - 所有维度初始值 0.5，confidence 0.0，避免 None 导致的空指针
- **Windows 支持**
  - `mnemos scheduler install-windows` — 注册 Task Scheduler 开机启动
  - `mnemos scheduler uninstall-windows` — 注销任务
- **画像校准 CLI**
  - `mnemos calibrate` — 交互式校准流程（1-5 分评分 + 置信度调整）
- **蒸馏重试机制**
  - `MAX_RETRIES = 3`，超期任务（24h）自动恢复为待处理
- **Daemon 预检**
  - `_run_preflight_checks()` 启动前检查目录、API、Agent 可用性、数据库
- **Agent 诊断命令**
  - `mnemos agent doctor` — 诊断所有 Agent 状态
  - `mnemos agent list` — 列出可用 Agent
  - `mnemos agent detect` — 检测宿主 Agent

### Fixed

- **Claude Code 适配器检测失败** — `is_available()` 增加 `~/.claude/settings.json` 和 `shutil.which("claude")` 检测路径
- **Caduceus datetime 导入缺失** — `from datetime import datetime` 补全
- **所有适配器 placeholder 格式错误** — judgment 值从 `keep/skip` 修正为 `knowledge/skill/skip`
- **Apollon timezone 导入缺失** — `delegate_distillation()` 使用的 `timezone.utc` 未导入
- **文件编码问题** — 13 处 `write_text()` 补全 `encoding="utf-8"`
- ** distill_queue 双通道问题** — 所有蒸馏路径统一通过 `enqueue() → HephaestusWorker.process_all() → AgentDelegate.delegate()`

### Changed

- **架构升级**：从单层处理模型升级为三层模型（Agent 适配器层 → 事件总线 → 核心服务层）
- **Agent 检测优先级**：Claude Code > Hermes > OpenClaw > OpenCode > Codex
- **README 重写**：新架构图、五 Agent 适配器说明、更新后的 CLI 命令列表

---

## [Unreleased] — 持续集成

### Knowledge-in-Action 闭环系统（2026-05-07）
> 从"知识沉淀"到"知识驱动行动"的完整闭环。不仅存储知识，更在实际工作中主动应用、复盘、迭代。

#### Added — 7个核心模块
- `core/task_classifier.py` — 通用任务分类器
  - 支持 coding/marketing/analysis/strategy/writing 五大类型及子类型
  - 关键词匹配 + 历史模式学习，置信度分层确认（>0.9静默/0.7-0.9提示/<0.7询问）
  - 自动提取预期目标（参与人数、转化率、预算等）
- `core/time_parser.py` — 时间解析器
  - 中文/英文相对时间解析（今天/明天/下周/下个月/明年Q1）
  - 周期性检测（weekly/biweekly/monthly/quarterly），加权滑动窗口
  - 返回 TimeWindow（immediate/short/medium/long/periodic）
- `core/pre_flight_injector.py` — 预加载注入器
  - 从 wiki/retrospectives/ 装载历史经验
  - 知识衰减排序（freshness_score，每版本衰减0.1）
  - 场景适配过滤（applies_when/not_applies_when）
  - 命中追踪（hit_count/last_hit）
- `core/in_process_guard.py` — 执行中守护
  - 三级策略：轻微偏差静默记录、中等偏差自然融入、严重偏差打断确认
  - 基于 checklist trigger_keywords 和 risk_patterns 匹配
- `core/auto_retrospective.py` — 自动复盘引擎
  - 触发检测：复盘关键词 + 自然结束检测
  - 预期 vs 实际对比，提取差异
  - checklist 使用情况问责记录
  - 提取新增教训
- `core/iteration_tracker.py` — 迭代版本追踪器
  - 基于复盘结果自动生成新版本（v1→v2→v3）
  - 知识衰减合并，更新 active 软链接
  - 归档旧版本到 .archive/
- `core/knowledge_scheduler.py` — 知识调度器
  - 使用 live_sync.db 存储远期/周期性任务
  - 启动补偿扫描，避免漏掉
  - 中期提前3天提醒，长期提前7天提醒

#### Added — 集成到 claude_integration.py
- `--session-start` 时自动调用 TaskClassifier + PreFlightInjector
- `--session-end --session-messages='...'` 时自动触发 Auto-Retrospective
- `--kia-check` 检查调度器中的到期提醒

#### Added — 复盘数据目录
```
wiki/retrospectives/
├── coding/
├── marketing/
├── analysis/
├── strategy/
└── writing/
```

#### Changed
- `claude_integration.py`: 导入 KIA 全部7个模块
- `claude_integration.py`: `get_context_for_claude()` 增加 KIA 知识装载逻辑

---

### Karpathy 蒸馏范式迁移（2026-05-03）
> 旧 Wiki 体系（Clean/Expand/L0-L9）全面废弃，改用 Karpathy LLM Wiki 范式。
> Memos 层无损保留全部上下文，LLM 主动蒸馏成结构化 Wiki。

#### Added
- 新建 `core/topic_splitter.py` — 轻量 LLM 话题切分器
  - 输入：一个 session 的消息列表
  - 输出：主题块列表（topic/start_msg/end_msg/type）
  - type：concept（有结论）/ thread（没结论）/ skip（跳过）
  - prompt 轻量，只切分不蒸馏，内容截断到 3000 字符控制费用
- 新建 `core/distiller.py` — 蒸馏器主模块
  - 概念文蒸馏：有结论的对话 → wiki/concepts/xxx.md
  - 话题串蒸馏：没结论的讨论 → wiki/threads/xxx.md
  - 质量自评（0-1），低于 0.3 丢弃
  - 内容指纹去重（MD5），已蒸馏过自动跳过
  - 自动更新 wiki/index.md 索引
  - 反向索引：记录 source_memos 到 wiki 的映射
- 新建 `core/wiki_quality.py` — 质量追踪系统（替代 L0-L9 热力）
  - 指标：完整性、新鲜度、矛盾数、引用深度、原子化程度
  - 状态：verified / draft / stale / conflicted
  - 存储：~/.claude/wiki_quality.db
  - 指纹表：distill_log（content_hash → wiki_path）
  - 反向链接：wiki_backlinks（谁引用了谁）
- 新建 `scripts/distill_worker.py` — 定时/手动蒸馏 Worker
  - 定时模式：`--run`，每天晚上 8:30 扫描增量
  - 手动模式：`--manual --uids uid1,uid2`
  - checkpoint 机制：上次成功时间 → 只处理新数据
  - RunAtLoad：开机自动补跑（错过的时间）
  - 从 ~/.zshrc 加载 MEMOS_TOKEN（launchd 不继承 shell env）
- 新建 `~/Library/LaunchAgents/com.memos.wiki.distill.plist`
  - 每天晚上 20:30 触发
  - 日志：/tmp/memos_distill.log

#### Changed
- `memos_sdk.py`: `mark_l1_processed()` 标记为废弃（空操作，打印警告）
  - 蒸馏体系用指纹表追踪状态，不在 Memos 上打 processed 标签
- `memos_sdk.py`: `batch_save()` 去掉 `type=clean-ingest` / `type=expand-ingest` 标签
- `claude_live_sync.py`: 去掉 `processed=false` 标签写入
  - 保留 source/thread/time/scope 描述性标签
  - `AUTO_INGEST_CLEAN = False`（改由 distill_worker 定时处理）
- `batch_clean_submit.py`: 整文件改为废弃提示
- `ingest_engine_service.py`: 整文件改为废弃提示

#### Removed
- 清掉旧 `wiki/` 目录全部内容（用户确认无价值）
- 删除 `~/.claude/wiki_heat_v4.db`
- 删除 `~/.claude/expand_v2.db`
- 废弃标签：`processed=true/false`, `ingest=wiki/skip`, `cleaned-to:{uid}`
- 废弃旧 L0-L9 热力追踪体系（代码保留但不再维护）

#### 待办（首尾工作）
- [ ] 停用旧 wiki 相关 launchd 任务（cold_demotion, draft_clean, expand_scan, heat_decay, health_check, synthesis_pipeline, weekly_report, wiki_tags_sync）
- [ ] 跑首次全量基线扫描（把历史 Memos 过一遍蒸馏）
- [ ] 观察一周 wiki 产出质量，调 prompt 和阈值

### Added
- `scripts/health_check.py`: 多库检查扩展
  - 新增 `ingest_engine.db` / `expand_v2.db` 健康检查
  - 新增 L0-L9 全分布统计 (`level_distribution`)
  - 新增衰减候选检测：15天未访问 (`stale_pages`) 与 60天沉睡 (`deep_sleeping`)
  - `check_database()` 返回结构改为按库分层的多字典

### Fixed
- `scripts/health_check.py`: 已全面迁移至 `wiki_heat_v4.db` / `wiki_heat` 表（v3 `heat_scoring.db` 彻底退役）

### Infrastructure (Wave B)
- 新建 `core/event_queue.py` — SQLite 轻量事件队列
  - 表结构：event_queue (id, event_type, entity_name, payload_json, dedupe_key, status, retry_count...)
  - 去重：同 dedupe_key 只保留一条，应用层自动跳过重复入队
  - 重试：失败 3 次后转 dead_letter，支持延迟重试（5min 退避）
  - 接口：enqueue / dequeue / mark_done / mark_retry_or_dead / get_stats / peek_dead_letters
- 新建 `core/rate_limiter.py` — Token Bucket LLM 限流器
  - 默认配置：并发 ≤3，每分钟 ≤30 调用，调用间隔 ≥1s
  - 支持阻塞 acquire() 与非阻塞 try_acquire()
  - 全局单例 `get_global_limiter()`，所有 LLM 调用点统一接入
- 新建 `scripts/event_worker.py` — Tier 2 队列消费 Worker
  - 轮询间隔 30s，批次 5 条
  - 已集成 rate_limiter，限流时自动回退重试
  - 处理器注册表当前为空，Wave C/D 逐步填充

### Architecture (Wave C)
- `ingest_engine.py`: `entity_source_count` 表加 `category` 字段 + 增量迁移
  - 新 schema: entity_name, source_count, created_at, last_updated, category
  - 旧数据回填: `UPDATE ... SET category = 'unknown' WHERE category IS NULL`
  - `_increment_entity_source_count()` 新增 `category` 参数，调用方传入 `refined.content_type`
- `core/cross_validator.py`: `_semantic_similarity()` 接入真 LLM
  - 优先调用 `LLMHelper.call_llm()`（已集成 Wave B rate_limiter）
  - prompt: 要求返回 0-1 数字评分
  - LLM 失败时 fallback 到关键词重叠，并打 WARNING 日志
- `core/llm_helper.py`: `LLMHelper` 新增通用 `call_llm()` 方法
  - 懒加载 Anthropic client，兼容 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`
  - 集成 `get_global_limiter()`，所有 LLM 调用自动过限流
- `document_processor.py`: 验证流程补全
  - `_call_claude_vision()` 改为走 `LLMHelper.call_llm()`，统一限流
  - `validate_extraction()` prompt 新增 `reject` 选项
  - `process_document_with_validation()` 新增 reject 路径
  - 新增 `save_to_rejected()` 方法：验证拒绝的文档保存到 `~/.claude/rejected_documents/`

### Event-Driven (Wave D)
- `core/expand_engine.py`: 新增 `evaluate_entity()` 单实体评估接口，复用批量逻辑
- `scripts/expand_scan.py`: 明确标注为 Tier 3 兜底扫描，主路径已迁移到事件驱动
- `ingest_engine.py`: `_increment_entity_source_count()` 成功后自动 enqueue `expand_eval` 事件
  - dedupe_key = `expand_eval:{entity_name}`，避免重复评估
- `core/wiki_heat_tracker.py`: `_add_heat()` 成功后自动 enqueue `sync_wiki_tags` 事件
  - dedupe_key = `sync:{page_id}`，同一页面只保留一个 pending 同步
- `scripts/sync_wiki_tags.py`: 提取 `sync_single_page()` 单页同步函数，供事件 handler 调用
- `scripts/event_worker.py`: 注册两个 handler
  - `expand_eval` -> `ExpandEngine.evaluate_entity()` + `ExpandExecutor.execute()`
  - `sync_wiki_tags` -> `sync_single_page()`（从 DB 读取最新 level/score）
- `synthesis_pipeline` 保持定时，后续 Phase 4 单独迁移（触发条件复杂，需观察）

### Cleanup (Wave E)
- 根目录 4 个 .py 搬入 `core/`
  - `entity_resolver.py` → `core/entity_resolver.py`
  - `ai_self_check.py` → `core/ai_self_check.py`
  - `conflict_merger.py` → `core/conflict_merger.py`
  - `cross_ai_tracker.py` → `core/cross_ai_tracker.py`
  - 更新 import：`ingest_engine.py` ×3, `ai_context_reader.py` ×1
- `HEAT_SYSTEM_COMPLETE_GUIDE.md` → `research/archive/HEAT_SYSTEM_COMPLETE_GUIDE.md`
  - 整份描述 v3 `heat_scoring` L2-C/B/A 旧系统，已过时

### AI Integration Fixes (改造2)
- `ai_memory_sync.py`: 标签统一（P0）
  - 4 处 `status=ready-for-ingest` → `processed=false`
  - 补 `source=hermes` / `source=openclaw` + `ingest=wiki`
  - Hermes sessions / OpenClaw files+chunks 同样补全
- 删除 `ingested` 死代码（P1）
  - `batch_clean_submit.py:61` / `ingest_engine_service.py:93,276`: 删掉 `or "ingested" in tags`
  - `core/namespaces.py:301`: special_tags 移除 `"ingested"`
- 新建 `scripts/migrate_status_tag.py`: 一次性迁移脚本
  - 遍历 Memos 所有 `status=ready-for-ingest` → `processed=false`
  - 防御：已有 `processed=true` 则跳过
- Token 泄露处理
  - `sync_all.sh`: 移除硬编码 token，改为从环境变量读取，缺失时报错退出
  - `AI_INTEGRATION.md`: 7 处明文 token 全部替换为 `$MEMOS_TOKEN`
  - 新增 "Token 安全配置" 章节，说明配置在 `~/.zshrc` + `chmod 600`
- 新建 `~/.claude/CLAUDE.md`: 跨会话记忆查询协议
  - 触发关键词：时间指代 / 会话续接 / 回忆复盘 / 历史引用
  - 执行命令：`claude_integration.py --session-start`
  - 禁止：知识查询类问题查 Memos、每轮都查、结果直接展示

### 已知遗留 (待处理)

- `ingest_engine.py` 当前 ~70KB（1700+ 行），仍属 God Class，可继续拆分（解析/落库/调用 LLM 三段独立化）
- 5 个 .py 文件 >30KB（document_processor, image_processor, ingest_engine, memos_sdk, wiki_reader）值得评估拆分
- `core/expand_executor._detect_conflicts` 为占位（空 pass），需等建立 `entity_sources` 关联表后才能真正接通 `cross_validator`
- Q4 watchdog 实时监听 Hermes：P0 修完后观察 3-5 天，按数据决定是否需要
- `synthesis_pipeline` 事件驱动化（Phase 4）待后续单独实施
- 文档审计报告需重新跑（P1/P2 后"4 个死代码模块"已全部被引用）

---

## 2026-05-02 — L0 浅下沉 + 文档归并

### Changed
- `core/wiki_heat_tracker.py` `MIN_SCORE`: -100 → -30
  - 设计动机：单次搜索命中 L0 页面 = wake +30 + 事件分 → 直接脱沉睡，"一次搜索就能回归正分"
  - 旧 -100 floor 下最坏要 9 次衰减触底，现在最多 6 次，沉睡更"浅"更可逆
  - 全仓 L0 描述同步更新（README、ARCHITECTURE、Mnemos-Auto-v4、research 系列）

### Removed
- `~/Desktop/ai/` 下 14 份镜像 .md（与 `~/memos-client/` 完全相同或更旧）
- `README_v4.md`（v4 早期简版，已被 `README.md` 完整版取代）
- `docs/EXPAND_2_0_ARCHITECTURE.md`（旧版，根目录有更新版）

---

## 2026-05-02 — P0-P3 重构周期收尾

### P0-1 — 清理 requirements.txt 错误依赖
- 移除项目实际未使用的依赖项

### P0-2 — `scripts/expand_scan.py` 迁移 V1→V2
- 旧 V1 引擎调用全部切到 V2 接口

### P0-3 — 热力等级单一事实源
- `core/wiki_heat_tracker.py` 引入 `LEVEL_RANGES` 元组表
- `_calculate_level` 用阈值表替换硬编码区间判断
- `PROMOTION_THRESHOLDS` 由 `LEVEL_RANGES` 自动推导
- 修改 L0 范围只需改一处常量

### P1-1 — 删除 3 个死代码函数
- `quality_assessor.assess_content_quality`
- `four_category_engine.classify_and_refine`
- `expand_engine_v2.evaluate_expand_candidates`

### P1-2 — 等级字符串比较改数值比较
- 新增静态方法 `WikiHeatTracker._level_int("L7") -> 7`
- 修复字典序反模式：旧代码 `"L10" < "L9"` 字面量比较出错
- 6 处比较点全量替换

### P1-3 — 删除 V1 模块并重命名 V2
- 删除：`core/expand_engine.py` (V1)
- 重命名：`core/expand_engine_v2.py` → `core/expand_engine.py`
- 全仓 6 处 `from core.expand_engine_v2 import` 已统一为 `from core.expand_engine import`
- 注：导出类名仍是 `ExpandEngineV2`（待去除 `V2` 后缀，见 [Unreleased]）

### P2-1 — `ingest_engine.py` God Class 渐进拆分
- 抽出 7 个去重/抽取纯函数 → `core/ingest_helpers.py`
  - `compute_fingerprint`, `is_duplicate_content`, `extract_concept_definition`
  - `extract_entities_fallback`, `extract_concepts_fallback`
  - `extract_entity_description`, `detect_wiki_reference_pollution`
- ingest_engine 内 `_extract_tech_entities` / `_parse_list` 删除（无调用点）
- 其余 7 个方法降级为 thin wrapper（保签名零侵入）

### P2-2 — 补三件套核心 unit test
- 新增 `tests/unit/` 67 个测试用例：
  - `test_wiki_heat_tracker.py` — `LEVEL_RANGES` SOT、`_calculate_level`、`_level_int`、L0/L9 边界、L10 前向兼容
  - `test_statement_classifier.py` — 句子切分、分类模式、级别准入
  - `test_cross_validator.py` — 事实抽取、语义相似度、硬事实冲突、置信度封顶
  - `test_ingest_helpers.py` — 七个抽取函数全覆盖
- 67 tests 0.040s 全绿

### P3-1 — 项目文档与桌面 ai 文档同步
- 全仓 L0/L9 数值描述同步
- 之后又触发 [2026-05-02 文档归并] 将桌面镜像清除

---

## 2026-04-30 — 死代码审计（已部分过期）

> 原报告：`memos_dead_code_audit_report.md`（合并后已删除）
> 状态：**报告时效已失，原文标记为"0 引用"的 4 个 .py 文件在 P1-P2 集成后均已被引用**

### 历史结论（仅供参考，不再适用）
| 模块 | 审计时状态 | 当前 (2026-05-02) |
|------|----------|-------------------|
| `entity_resolver.py` | 0 引用 | ✅ `ingest_engine.py:191` 实例化、`:960/971` 调用 |
| `ai_self_check.py` | 0 引用 | ✅ `ingest_engine.py:192` 实例化 |
| `conflict_merger.py` | 0 引用 | ✅ `ingest_engine.py:193` 实例化、`:1056/1088` 调用 |
| `cross_ai_tracker.py` | 0 引用 | ✅ `ai_context_reader.py:63` 实例化、`:225/270` 调用 |

### 历史指标（审计当时）
- 总 Python 文件数：43
- 总函数/方法数：~350+
- 标记的死代码模块：4 个
- 标记的死代码方法：2 个

---

## 2026-04-29 — 四大类信息识别集成（v4 工程）

> 原报告：`INTEGRATION_REPORT.md`（合并后已删除）

### Added
- `config/entity_config.yaml` — 四大类分类完整配置
- `core/four_category_engine.py` — Layer1-4 四层识别引擎
  - Layer1: 元数据标签识别
  - Layer2: 结构特征匹配
  - Layer3: LLM 语义识别
  - Layer4: 动态权重融合
- `core/llm_helper.py`（既有）

### Changed (`ingest_engine.py`)
- 引入 `FourCategoryEngine` 实例化
- `_extract_categorized_content` 新方法：调用引擎做智能分类提炼
- `_process_clean` / `_process_expand` / `_create_source_page` 三处改造：分类信息接入
- 后向兼容保留

---

## 2026-04-29 — v3→v4 架构审查（heat_scoring → wiki_heat_tracker 迁移）

> 原报告：`ARCHITECTURE_REVIEW_v4.md`（合并后已删除）

### 关键问题（已修复）
- 🔴 新旧热力系统并存：旧 `heat_scoring.py` (L2-C/B/A, 5 级) vs 新 `wiki_heat_tracker.py` (L0-L9, 10 级)
- 🔴 双数据库：`heat_scoring.db` vs `wiki_heat_v4.db`
- 🔴 7 个文件仍引用旧系统（已在 P1-3 全量收敛到新系统）：
  ```
  ingest_engine.py:35, verify_automation.py:39, heat_integration.py:13,
  heat_monitor.py:16, l1_refinement.py:32, claude_live_sync.py:22,
  cross_ai_tracker.py:18
  ```

### 关键风险（已修复）
- 🔴 `wiki_heat_tracker.on_ai_search_hit` 潜在无限递归（边界条件已加 guard）

---

## 2026-04-30 — v3 期初次架构审查

> 原报告：`ARCHITECTURE_REVIEW_REPORT.md`（合并后已删除）

### 已识别矛盾点（部分修复）
1. 🔴 `DocumentProcessor` 字段缺失：`extraction.validation_status / needs_review / review_reason` 在 `knowledge_inbox.py:313-360` 被引用，但 dataclass 未定义
2. 🔴 `DocumentProcessor.save_to_memos_with_review()` 被 `knowledge_inbox.py:322` 调用但方法不存在
3. 🔴 `ImageProcessor.save_to_memos` 签名不一致
4. 🟡 `ingest_engine.py:789, 874` 动态导入路径脆弱
5. 🟡 `DocumentProcessor` 缺验证机制（图片处理器有完整 Claude Vision 验证流程，文档处理器无）
6. 🟡 配置不一致：`ai_context_reader.py` 用 `Path.home() / "memos-client"`，其他用 `os.path.expanduser("~/memos-client")`

> 注：上述 1-3 项部分仍存在；建议在下一轮 doc-processor 重构时一并解决。

---

## 历史版本

### v4.0 — 全自动架构（2026-04 中期）
- 热力追踪迁移至 Wiki 层（`wiki_heat_tracker.py`）
- AI 仅搜索 Wiki，不搜索 Memos 原始草稿池
- 10 级 L0-L9 体系，L9 封顶 500
- 衰减 + 冷降级双机制

### v3.0 — 人工审核版（2026-04 早期）
- 热力追踪在 Memos 层
- 6 级 L1-L5 + L6 体系
- 现已被 v4 完全取代

---

*本文件为 SOT — 不再维护多份 ARCHITECTURE_REVIEW / INTEGRATION_REPORT / dead_code_audit 散文件。*
