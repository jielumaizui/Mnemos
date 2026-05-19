# Memos-Client 模块深度分析报告

## 1. ai_memory_sync.py (943行) - AI记忆双向同步

### 核心类/函数
- `MemorySyncBridge` - 记忆同步桥接器主类
  - `detect_wiki_reference()` - 检测Wiki自引用 ([[...]] 或 hash=)
  - `_guard_tags()` - 自引用防护，标记 do-not-ingest
  - `is_noise_message()` - 噪音消息检测（中文确认/英文确认/纯表情）
  - `sync_to_memos()` - 同步到Memos
  - `sync_from_memos()` - 从Memos拉取

### 外部依赖
- **第三方**: 无特殊（标准库为主）
- **内部**: `memos_sdk.MemosClient`, `core.ingest_helpers.score_message_quality`

### LLM调用点
- 无直接LLM调用

### 与其他缺失模块的调用关系
- → `memos_sdk.MemosClient` (Memos API客户端)
- → `core.ingest_helpers.score_message_quality` (消息质量评分)

### 跨平台潜在问题
- **路径**: `_SYNC_LOG_DB = Path("~/.claude/ai_sync_log.db").expanduser()` 硬编码
- **编码**: 中文字符在正则中直接使用，需确保UTF-8编码
- **已废弃**: 文件头标记 `@deprecated`，已被 `claude_realtime_sync.py` 等替代

---

## 2. claude_live_sync.py (640行) - Claude实时同步

### 核心类/函数
- `ClaudeSessionHandler(FileSystemEventHandler)` - 会话文件变化处理器
  - `_init_db()` - 初始化防重SQLite数据库
  - `_load_processed_sessions()` - 加载历史处理记录
  - `_is_content_duplicate()` - 增强防重检查(MD5)
  - `on_modified()` / `on_created()` - watchdog事件回调
  - `_process_session_file()` - 处理session文件
  - `_save_to_memos()` - 保存到Memos
  - `_debounce_process()` - 防抖处理(5秒)

### 外部依赖
- **第三方**: `watchdog` (文件监控)
- **内部**: `memos_sdk.MemosClient`, `task_id_parser.TagBuilder/TaskIdParser`, `core.ingest_helpers.is_noise_message`

### LLM调用点
- 无直接LLM调用

### 与其他缺失模块的调用关系
- → `memos_sdk.MemosClient`
- → `task_id_parser.TagBuilder`, `task_id_parser.TaskIdParser`
- → `core.ingest_helpers.is_noise_message`
- → `ingest_engine.IngestEngine` (软依赖，AUTO_INGEST_CLEAN已废弃)

### 跨平台潜在问题
- **文件监控**: watchdog在macOS/Linux/Windows行为不同
- **路径**: `Path.home() / ".claude" / "live_sync.db"` 硬编码
- **线程**: 使用threading.Timer做debounce，进程退出时可能丢失pending
- **进程管理**: Observer守护进程需独立管理

---

## 3. ingest_engine.py (1860行) - 摄入引擎

### 核心类/函数
- `IngestTask` - 摄入任务数据类
- `IngestEngine` - 摄入引擎主类
  - `ingest()` - 主入口，分Clean/Expand/Manual三种模式
  - `_process_clean()` - Clean模式：新实体创建
  - `_process_expand()` - Expand模式：追加到已有实体
  - `_process_manual()` - Manual模式：用户指定目标
  - `_create_wiki_page()` - 创建Wiki页面
  - `_update_entity_page()` - 更新实体页面
  - `_append_to_concept()` - 追加概念内容
  - 四层污染防护:
    - `_check_circular_pollution()` - 循环污染检测
    - `_check_self_reference()` - 自引用检测
    - `_check_wiki_reference_pollution()` - Wiki引用污染
    - `_check_content_duplication()` - 内容去重
  - 写入锁: `_acquire_write_lock()` / `_release_write_lock()`
  - 冻结机制: `_freeze_entity()` / `_thaw_entity()` / `_is_entity_frozen()`
  - 异常检测: `_check_update_frequency()` / `_record_conflict()` / `_check_entity_anomalies()`
  - 多源验证: `_check_single_source_constraint()`
  - Background Review: `_enqueue_background_review()`

### 外部依赖
- **第三方**: 无特殊
- **内部**: `core.ingest_helpers` (extract_concept_definition, detect_wiki_reference_pollution, check_wiki_self_reference, score_message_quality), `core.wiki_metrics.WikiMetrics`, `config` 模块

### LLM调用点
- 无直接LLM调用（LLM调用在上游蒸馏模块中）

### 与其他缺失模块的调用关系
- → `core.ingest_helpers` (多个辅助函数)
- → `core.wiki_metrics.WikiMetrics` (质量/热力追踪)
- ← `knowledge_inbox.py` (软依赖调用)
- ← `run_distill_from_memos.py` (通过Orchestrator间接调用)

### 跨平台潜在问题
- **路径**: Wiki路径 `~/Documents/Obsidian Vault/wiki/` 硬编码macOS
- **SQLite**: 多处直接sqlite3.connect，线程安全风险（用了write_locks但非全局锁）
- **编码**: 文件读写均用UTF-8，需确保一致
- **进程管理**: subprocess.run调用外部脚本
- **定时**: 与Config.INGEST_RETRY_TIMES/DELAY耦合

---

## 4. knowledge_inbox.py (877行) - 知识收件箱

### 核心类/函数
- `InboxFile` - 收件箱文件记录数据类
- `KnowledgeInboxProcessor` - 知识收件箱处理器
  - `scan_inbox()` - 扫描收件箱目录
  - `process_file()` - 处理单个文件
  - `_process_text_file()` - 处理文本文件
  - `_process_ebook()` - 处理电子书(.epub等)
  - `_process_image()` - 处理图片(OCR)
  - `_process_url()` - 处理URL
  - `_process_document()` - 处理文档(.pdf/.docx等)
  - `generate_report()` - 生成处理报告
  - 验证状态管理: pending/processing/done/error

### 外部依赖
- **第三方**: `ebooklib` (软依赖), `PIL/Pillow` (图片处理,软依赖)
- **内部**: `memos_sdk.MemosClient`, `task_id_parser.TaskIdParser/TagBuilder`, `document_processor.DocumentProcessor`

### LLM调用点
- 无直接LLM调用

### 与其他缺失模块的调用关系
- → `memos_sdk.MemosClient`
- → `task_id_parser` 模块
- → `document_processor.DocumentProcessor`
- → `core.wiki_metrics.WikiHeatTracker` (软依赖)
- → `ingest_engine.IngestEngine` (软依赖)

### 跨平台潜在问题
- **路径**: `Path.home() / "Desktop" / "到家" / "ai" / "knowledge_inbox"` - 严重硬编码中文路径
- **桌面路径**: macOS vs Linux vs Windows桌面路径不同
- **文件锁**: 无显式文件锁，并发处理可能冲突
- **电子书**: ebooklib可能不支持mobi/azw3格式

---

## 5. distillation_agent.py (251行) - 蒸馏智能体

### 核心类/函数
- `_detect_session_type()` - 根据关键词判断session类型(coding/marketing/analysis/strategy/writing/review)
- `DISTILL_PROMPT_TEMPLATE` - 蒸馏prompt模板（6种知识类型: decision/pattern/pitfall/snippet/reference/todo）
- CLI接口: `--next` / `--done` / `--list` / `--fail`

### 外部依赖
- **第三方**: 无
- **内部**: `core.distillation_queue` (get_next/mark_done/mark_failed/list_pending), `core.distillation_prompts.TYPE_DISTILL_PROMPTS`

### LLM调用点
- 无直接LLM调用（设计为由Claude Code Agent执行，Agent自身就是LLM）

### 与其他缺失模块的调用关系
- → `core.distillation_queue` (队列管理)
- → `core.distillation_prompts` (prompt模板)
- ← `run_distill_from_memos.py` (通过Orchestrator间接)

### 跨平台潜在问题
- **路径**: `WIKI_DIR = Path.home() / "Documents" / "Obsidian Vault" / "wiki"` 硬编码
- **sys.path**: `sys.path.insert(0, str(Path.home() / "memos-client"))` 硬编码

---

## 6. run_distill_from_memos.py (261行) - 从Memos触发蒸馏

### 核心类/函数
- `fetch_memos()` - 从Memos API分页拉取所有数据
- `extract_session_data()` - 从clean-refined内容提取session JSON
- `SKIP_PATTERNS` - 28条过滤正则（排除AI输出/代码/工具调用等）
- `filter_sessions()` - 过滤session
- `distill_session()` - 蒸馏单个session
- `main()` - 主流程：拉取→过滤→蒸馏→主控循环

### 外部依赖
- **第三方**: 无（仅用urllib标准库）
- **内部**: `core.orchestrator.Orchestrator`

### LLM调用点
- 无直接LLM调用

### 与其他缺失模块的调用关系
- → `core.orchestrator.Orchestrator` (蒸馏→Wiki全流程)

### 跨平台潜在问题
- **HTTP**: 使用urllib.request，无重试/超时机制
- **硬编码**: `CURRENT_SESSION = "16821fc5-09bf-4c4d-b701-4e29f72957d0"` 硬编码
- **路径**: `WIKI_BASE` 硬编码macOS路径
- **编码**: JSON解析无encoding参数，依赖默认UTF-8

---

## 7. knowledge_graph.py (722行) - 知识图谱

### 核心类/函数
- `KnowledgeGraph` - 知识图谱管理器
  - `add_relation()` - 添加关系（含反向关系自动维护）
  - `remove_relation()` - 删除关系
  - `get_relations()` - 获取出边关系
  - `get_incoming_relations()` - 获取入边关系
  - `discover_relations()` - 自动关系发现（基于关键词/链接/Wiki引用）
  - `apply_discovered()` - 批量应用发现的关系
  - `get_related_cluster()` - 获取关联集群
  - `find_path()` - A*路径查找
  - `detect_conflicts()` - 冲突检测（builds_on+contradicts, 循环replaces等）
  - `get_contradiction_pairs()` - 获取矛盾对
  - `export_mermaid()` - 导出Mermaid图
  - `export_dataview_query()` - 导出Dataview查询
  - `export_frontmatter_relations()` - 导出frontmatter关系
  - `get_stats()` / `get_hub_pages()` - 统计
- `build_graph_for_wiki()` - 便捷函数，全量扫描构建

### 外部依赖
- **第三方**: `yaml` (frontmatter解析)
- **内部**: `core.relation_schema` (Relation, RelationType, RelationEvidence)

### LLM调用点
- 无直接LLM调用

### 与其他缺失模块的调用关系
- → `core.relation_schema` (类型定义)
- ← `core.orchestrator.Orchestrator.run_graph()` (阶段3)
- ← `heartbeat.py` (间接通过wiki_metrics)

### 跨平台潜在问题
- **SQLite**: `~/.claude/knowledge_graph.db` 硬编码
- **YAML**: 可选依赖，缺失时frontmatter解析失败
- **路径**: Wiki路径硬编码

---

## 8. relation_schema.py (352行) - 关系定义

### 核心类/函数
- `RelationType(str, Enum)` - 16种关系类型枚举
  - 层级: BUILDS_ON, SPECIALIZES, GENERALIZES, PART_OF, HAS_PART
  - 因果: CAUSES, DEPENDS_ON, PREREQUISITE_FOR, SOLVES
  - 演化: REPLACES, EVOLVED_FROM, SUPERCEDED_BY
  - 对比: CONTRADICTS, ALTERNATIVE_TO, SIMILAR_TO
  - 元关系: REFERENCES, INSTANCE_OF
- `RELATION_META` - 关系元数据字典（反向关系、对称性、传递性、描述、示例）
- `Relation` - 关系数据类
- `RelationEvidence` - 证据数据类
- `validate_relation()` - 验证关系合法性
- `get_reverse_type()` - 获取反向关系类型

### 外部依赖
- **第三方**: 无
- **内部**: 无

### LLM调用点
- 无

### 与其他缺失模块的调用关系
- ← `core.knowledge_graph.py` (核心依赖)

### 跨平台潜在问题
- 纯数据定义模块，无跨平台问题

---

## 9. job_scheduler.py (634行) - 调度器

### 核心类/函数
- `JobStatus(Enum)` - 任务状态(PENDING/RUNNING/SUCCESS/FAILED/TIMEOUT/SKIPPED/CANCELLED)
- `JobTrigger(Enum)` - 触发方式(CRON/MANUAL/DEPENDENCY/RETRY)
- `JobConfig` - 任务配置数据类
- `JobRun` - 执行记录数据类
- `CronParser` - 简化Cron解析器
  - `is_due()` - 检查是否到期
  - `get_next_run()` - 获取下次执行时间
- `JobScheduler` - 调度器主类
  - `register_job()` - 注册任务
  - `run_job()` - 执行任务（带依赖检查+重试+超时）
  - `run_due_jobs()` - 批量执行到期任务
  - `run_job_chain()` - 顺序执行任务链
  - `_check_dependencies()` - 依赖检查
  - `_execute_once()` - 单次执行（subprocess.run + timeout）
  - `health_check()` / `get_stats()` - 监控
- `get_default_scheduler()` - 全局单例

### 外部依赖
- **第三方**: 无
- **内部**: 无

### LLM调用点
- 无

### 与其他缺失模块的调用关系
- 独立模块，无内部依赖
- ← `heartbeat.py` (可能间接使用)
- ← `ingest_engine.py` (可能通过Config关联)

### 跨平台潜在问题
- **Cron解析**: `_match_field` 中 `now.weekday() + 1 % 7` 运算符优先级BUG（1%7=1, 应为 (now.weekday()+1)%7）
- **subprocess**: 硬编码 `python3`，Windows可能需要 `python`
- **SQLite**: `~/.claude/job_scheduler.db` 硬编码
- **脚本目录**: `~/memos-client/scripts` 硬编码
- **线程**: threading.local + threading.Lock，多线程安全
- **signal**: import了signal但未使用

---

## 10. orchestrator.py (631行) - 编排器

### 核心类/函数
- `Orchestrator` - 主控编排器
  - 14个阶段方法:
    - `run_distill()` - 蒸馏
    - `run_dna()` - DNA指纹
    - `run_graph()` - 知识图谱
    - `run_immune()` - 免疫系统
    - `run_entropy()` - 熵减
    - `run_stress()` - 压力测试
    - `run_falsify()` - 可证伪标记
    - `run_dark()` - 暗知识挖掘
    - `run_entangle()` - 量子纠缠
    - `run_shadow()` - 影子页面
    - `run_capsule()` - 时间胶囊
    - `run_snapshot()` - 版本快照
    - `run_push()` - 推送引擎
    - `run_profile()` - 知识画像
  - `run_full()` - 完整循环（14阶段串联）
  - `generate_report()` - Markdown报告生成

### 外部依赖
- **第三方**: 无
- **内部**: 延迟import 14个core模块:
  - `core.distillation_engine.DistillationEngine`
  - `core.knowledge_dna.DNAEngine`
  - `core.knowledge_graph.KnowledgeGraph`
  - `core.knowledge_immune.KnowledgeImmuneSystem`
  - `core.entropy_engine.EntropyEngine`
  - `core.stress_test.StressTestEngine`
  - `core.falsifiability_marker.FalsifiabilityMarker`
  - `core.dark_knowledge.DarkKnowledgeMiner`
  - `core.quantum_entanglement.QuantumEntanglement`
  - `core.shadow_page.ShadowPageManager`
  - `core.time_capsule.TimeCapsule`
  - `core.version_time_travel.VersionTimeTravel`
  - `core.predictive_push.PredictivePushEngine`
  - `core.knowledge_profile.ProfileGenerator`

### LLM调用点
- 无直接LLM调用（LLM调用在被编排的子模块中）

### 与其他缺失模块的调用关系
- → 14个core子模块（延迟import）
- ← `run_distill_from_memos.py`

### 跨平台潜在问题
- **路径**: `Path.home() / "Documents" / "Obsidian Vault" / "wiki"` 硬编码macOS
- **延迟import**: 所有子模块import在方法内，任一缺失不影响其他阶段
- **异常处理**: 每阶段try/except，失败不阻断后续阶段

---

## 11. heartbeat.py (414行) - 心跳守护

### 核心类/函数
- `HeartbeatDaemon` - 心跳守护器
  - `check_config_changes()` - 配置文件变更检测（MD5校验）
  - `check_wiki_changes()` - Wiki目录变更检测（mtime+size对比）
  - `on_config_changed()` - 配置变更处理
  - `on_wiki_changed()` - Wiki变更处理 → 更新wiki_metrics
  - `run_decay()` - 热力衰减
  - `run_full_index()` - 全量索引扫描
  - `run_once()` - 单次心跳检查
  - `run_loop()` - 持续运行守护循环（while True + sleep）
  - `get_stats()` - 统计
  - `_parse_wiki_content()` - 解析frontmatter

### 外部依赖
- **第三方**: `yaml` (可选依赖)
- **内部**: `core.wiki_metrics.get_default_metrics`

### LLM调用点
- 无

### 与其他缺失模块的调用关系
- → `core.wiki_metrics.get_default_metrics` (热力追踪+衰减)

### 跨平台潜在问题
- **守护进程**: `run_loop()` 使用 `while True + time.sleep()`，无进程管理(daemon化/supervisor/systemd)
- **SQLite**: `~/.claude/heartbeat_state.db` 硬编码
- **Wiki子目录**: `WIKI_SUBDIRS` 硬编码列表
- **文件监控**: 基于mtime+size，精度秒级，可能漏检快速连续修改
- **编码**: `file_path.read_text(encoding="utf-8")` 明确指定，OK

---

## 12. content_formatter.py (430行) - 内容格式化

### 核心类/函数
- `ExpressionForm(Enum)` - 7种表达形式(MARKDOWN_LIST/COMPARISON_TABLE/MERMAID_FLOW/CONFIG_BLOCK/PRO_CON_TABLE/CHECKLIST/TREE_DIAGRAM)
- `FormatSuggestion` - 格式建议数据类
- `ContentFormatter` - 内容格式化器
  - `DETECTION_RULES` - 6类检测规则（对比/流程/配置/正反/检查清单/决策树）
  - `detect_form()` - 检测最佳表达形式（正则+关键词+权重评分）
  - `format_content()` - 格式化入口
  - `_to_comparison_table()` - 转对比表格
  - `_to_mermaid_flow()` - 转Mermaid流程图
  - `_to_config_block()` - 转YAML配置块
  - `_to_pro_con_table()` - 转正反对照表
  - `_to_checklist()` - 转检查清单
  - `_to_tree_diagram()` - 转树形决策图
- `auto_format()` / `detect_best_form()` - 便捷函数

### 外部依赖
- **第三方**: 无
- **内部**: 无

### LLM调用点
- 无（纯规则引擎，启发式检测）

### 与其他缺失模块的调用关系
- 独立模块，无依赖
- ← `ingest_engine.py` 或蒸馏模块可能调用

### 跨平台潜在问题
- 纯文本处理模块，无跨平台问题
- **中文正则**: 大量中文关键词匹配，依赖UTF-8编码环境

---

## 13. wiki_metrics.py (662行) - Wiki度量

### 核心类/函数
- `KnowledgeStage(Enum)` - P0/P1/P2/P3
- `HeatLevel(Enum)` - cold/warm/hot
- `QualityLevel(Enum)` - excellent/good/acceptable/poor
- 工具函数: `compute_evidence_level()`, `compute_knowledge_stage()`, `compute_heat_level()`, `hash_query()`, `quick_quality_score()`
- `PageMetrics` - 页面度量数据类
- `WikiMetrics` - 度量中心
  - `upsert_page()` - 插入/更新页面指标
  - `get_page()` - 获取页面指标
  - `list_pages()` - 列出页面（支持过滤）
  - `assess_quality()` - 质量评估(4维度: 密度/结构/链接/丰富度)
  - `update_heat()` - 热力更新(read/search_hit/citation/edit)
  - `decay_all()` - 全局热力衰减
  - `add_relation()` / `get_relations()` - 页面关系
  - `get_merge_candidates()` - 合并候选（按主题前缀聚类）
  - `mark_deprecated()` / `mark_merged()` - 状态标记
  - `get_summary()` / `generate_report()` - 报告
- `get_default_metrics()` / `quick_assess()` - 便捷函数

### 外部依赖
- **第三方**: 无
- **内部**: 无

### LLM调用点
- 无（质量评分基于规则，非LLM）

### 与其他缺失模块的调用关系
- ← `heartbeat.py` (热力衰减+索引)
- ← `ingest_engine.py` (质量/热力/阶段更新)
- ← `core.knowledge_graph.py` (间接)

### 跨平台潜在问题
- **SQLite**: `~/.claude/wiki_metrics.db` 硬编码
- **时区**: `datetime.now()` 无时区处理，跨时区可能不一致
- **JSON序列化**: `json.dumps(..., ensure_ascii=False)` 正确处理中文

---

## 14. llm_helper.py (362行) - LLM辅助

### 核心类/函数
- `MaturityAssessment` - 感悟成熟度评估结果数据类
- `CodeOperation` - 代码操作理解结果数据类
- `LLMHelper` - LLM辅助提炼类
  - `client` - 懒加载Anthropic客户端
  - `call_llm()` - 通用LLM调用（Anthropic API）
  - `assess_insight_maturity()` - 评估感悟成熟度（当前为规则后备，未调用LLM）
  - `understand_code_operations()` - 理解代码操作序列（纯规则解析）
  - `classify_content_semantic()` - 语义分类（当前为关键词密度，注释说"实际应调用LLM"）
- `assess_maturity()` / `understand_operations()` - 便捷函数

### 外部依赖
- **第三方**: `anthropic` (懒加载)
- **内部**: 无

### LLM调用点
- `call_llm()` - Anthropic Messages API (`client.messages.create`)
  - 模型: `claude-sonnet-4-6` (默认)
  - API Key: `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`
  - Base URL: `ANTHROPIC_BASE_URL` (可选)
- **注意**: 当前所有业务方法(assess_insight_maturity/understand_code_operations/classify_content_semantic)均为规则后备实现，未实际调用LLM

### 与其他缺失模块的调用关系
- 独立模块，无内部依赖
- ← 蒸馏模块/分类模块可能调用

### 跨平台潜在问题
- **API Key**: 从环境变量读取，需确保环境配置
- **网络**: Anthropic API调用需网络，无重试机制
- **模型名**: `claude-sonnet-4-6` 可能需要更新

---

## 15. credential_pool.py (593行) - 凭证池

### 核心类/函数
- `Provider(Enum)` - ANTHROPIC/OPENAI/SILICONFLOW/GEMINI
- `KeyStatus(Enum)` - ACTIVE/COOLING/EXPIRED/DISABLED
- `Credential` - 凭证数据类（含to_dict/success_rate/is_available属性）
- `CredentialPool` - API Key池管理器
  - `COOLDOWN_MINUTES` - 冷却配置(rate_limit:1m/server_error:5m/auth_error:60m/timeout:2m)
  - `_load_from_env()` - 从环境变量加载key
  - `add_key()` / `remove_key()` - Key管理
  - `get_key()` - 获取可用key（weighted/round_robin/random策略）
  - `mark_success()` / `mark_failure()` - 调用结果记录
  - `_classify_error()` - 错误分类
  - `_weighted_select()` / `_round_robin_select()` - 选择策略
  - `health_check()` - 健康检查
  - `reset_cooldown()` - 手动重置冷却
- `get_default_pool()` - 全局单例（线程安全）

### 外部依赖
- **第三方**: 无
- **内部**: 无

### LLM调用点
- 无

### 与其他缺失模块的调用关系
- ← `core.auxiliary_client.py` (核心依赖)

### 跨平台潜在问题
- **SQLite**: `~/.claude/credential_pool.db` 硬编码
- **安全**: api_key明文存储在SQLite中
- **线程**: threading.local + threading.Lock，线程安全
- **环境变量**: 4组环境变量映射，需确保配置

---

## 16. auxiliary_client.py (413行) - 辅助客户端

### 核心类/函数
- `ChatResponse` - 标准化响应数据类
- `ChatRequest` - 标准化请求数据类
- `ProviderAdapter` - Provider适配器基类
- `AnthropicAdapter` - Anthropic Claude适配器
- `OpenAIAdapter` - OpenAI适配器
- `SiliconFlowAdapter` - SiliconFlow适配器(OpenAI兼容API)
- `AuxiliaryClient` - 多Provider降级客户端
  - `DEFAULT_CHAIN` - 降级链路: ANTHROPIC→OPENAI→SILICONFLOW
  - `DEFAULT_MODELS` - 默认模型映射(claude-sonnet-4-6/gpt-4/deepseek-ai/DeepSeek-V2.5)
  - `chat()` - 发送聊天请求（自动降级）
  - `quick_chat()` - 快速单轮对话
  - `get_last_provider()` / `get_last_error()` / `get_available_providers()` - 状态查询
- `get_default_client()` - 全局单例

### 外部依赖
- **第三方**: `anthropic` (AnthropicAdapter), `openai` (OpenAIAdapter/SiliconFlowAdapter)
- **内部**: `core.credential_pool.CredentialPool/Provider/get_default_pool`

### LLM调用点
- `AnthropicAdapter.chat()` - `anthropic.Anthropic` → `client.messages.create()`
- `OpenAIAdapter.chat()` - `openai.OpenAI` → `client.chat.completions.create()`
- `SiliconFlowAdapter.chat()` - `openai.OpenAI` → `client.chat.completions.create()` (base_url=siliconflow)
- 自动降级: 一个Provider失败自动切换下一个

### 与其他缺失模块的调用关系
- → `core.credential_pool` (核心依赖)

### 跨平台潜在问题
- **第三方库**: anthropic/openai需安装，缺失时运行时错误
- **网络**: API调用需外网访问，SiliconFlow需中国网络
- **模型名**: 硬编码默认模型名，需随API更新

---

## 模块间依赖关系图

```
                        ┌─────────────────┐
                        │  orchestrator   │  (14阶段主控)
                        └───────┬─────────┘
                                │ (延迟import 14个子模块)
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
        knowledge_graph   knowledge_dna   distillation_engine
                │               │               │
                ▼               │               │
        relation_schema        │               │
                                │               │
    ┌───────────────────────────┼───────────────┤
    │                           │               │
    ▼                           ▼               ▼
run_distill_from_memos    ingest_engine    knowledge_immune
    │                       │   │           entropy_engine
    │                       │   │           stress_test
    │                       ▼   ▼           dark_knowledge
    │                  wiki_metrics          quantum_entanglement
    │                       ▲               shadow_page
    │                       │               time_capsule
    │                  heartbeat            version_time_travel
    │                                       predictive_push
    │                                       falsifiability_marker
    │                                       knowledge_profile
    │
    ├─► ai_memory_sync ─► memos_sdk, ingest_helpers
    │
    ├─► claude_live_sync ─► watchdog, memos_sdk, ingest_helpers
    │
    ├─► knowledge_inbox ─► memos_sdk, document_processor, ebooklib
    │
    ├─► distillation_agent ─► distillation_queue, distillation_prompts
    │
    ├─► job_scheduler (独立)
    │
    ├─► content_formatter (独立)
    │
    ├─► llm_helper ─► anthropic
    │
    ├─► auxiliary_client ─► credential_pool ─► anthropic/openai
    │
    └─► wiki_metrics (独立，被多模块依赖)
```

## 共性跨平台问题汇总

1. **路径硬编码**: 几乎所有模块硬编码macOS路径 (`~/Documents/Obsidian Vault/wiki/`, `~/memos-client/`, `~/.claude/`)
2. **SQLite散落**: 6个独立SQLite数据库文件 (`ai_sync_log.db`, `live_sync.db`, `job_scheduler.db`, `heartbeat_state.db`, `wiki_metrics.db`, `knowledge_graph.db`, `credential_pool.db`)
3. **编码处理**: 大量中文正则/关键词匹配，依赖UTF-8环境
4. **进程管理**: heartbeat用while True循环，无daemon化；job_scheduler用subprocess.run硬编码python3
5. **线程安全**: wiki_metrics/credential_pool用threading.local；ingest_engine用自定义write_locks
6. **API Key管理**: 分散在环境变量和明文SQLite中
7. **Cron解析BUG**: job_scheduler中weekday计算运算符优先级错误
8. **LLM调用分散**: llm_helper有call_llm但未使用，auxiliary_client独立实现降级链
