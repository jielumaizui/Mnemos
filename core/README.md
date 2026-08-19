# `core/` 模块地图

本目录是 Mnemos 的 **Layer 1 / Layer 2 核心域**，负责知识采集、蒸馏、存储、搜索、画像、事件总线与 Agent 行为闭环。所有模块不得直接导入 `integrations.*`（`core/cli` 作为入口适配器短期豁免，见 `docs/core-integrations-dependencies.md`）。

## 按分层划分

| 分层 | 模块/目录 | 职责 |
|---|---|---|
| 配置与入口 | `config.py` | 统一配置管理：JSON + 环境变量 + 默认值，提供 `ConfigProvider` 协议支持依赖注入。 |
| 配置与入口 | `mnemos_cli.py` 调用 | `cli/` | 命令行入口与子命令懒加载。 |
| 事件总线 | `mnemos_bus.py` | SQLite + 内存队列的事件总线：发布/订阅、死信、重试、启动恢复、并行分发。 |
| 同步框架 | `sync_framework/` | 原始对话采集、文件摄入、CaptureQueue/Worker、canonical current-revision typed reader、Raw provenance terminal receipt、存储后端注册表。 |
| 存储后端 | `app/` | Obsidian 页面读写、RawIndex、搜索、争议评分、盲区发现。 |
| 蒸馏 | `hephaestus/` | 七层蒸馏流水线：分块、LLM 调用、提取、校验、合并、Wiki 写入。 |
| 知识图谱 | `kia/` | KIA（Knowledge-in-Action）闭环：实体/关系管理、Chronos 调度、guard/recap、热插拔模块。详见 `core/kia/README.md`。 |
| 认知层 | `cognitive/` | Observation Engine：从 canonical Raw current revision 与 Wiki 提取七维客观观察；Raw Markdown 只作显式 v2 compatibility audit，不能回退为运行输入。 |
| 认知图谱 | `cognitive_graph/` | 跨层概念图谱、canonical node、关系投影。 |
| 画像 | `persona/` | 能量/认知/价值三层画像雷达、信号收集、漂移检测。 |
| 反射 | `reflection/` | Layer 5 外循环：Insight 生成、反馈收集、认知变迁、消费者管道。 |
| 嵌入 | `embeddings/` | 向量索引、关系上下文 embedding、reranker 客户端。 |
| 评分 | `scoring/` | COLD/WARM/HOT 三阶段评分、自适应评分器 v2、资源预算。 |
| 证据 | `evidence/` | 证据图、来源追溯。 |
| 安全与访问 | `access_policy.py` | 跨 Agent / project / framework / global scope 的访问控制。 |
| 安全与访问 | `llm_key_pool.py` | 同 Provider 多 API key 内存池：加权选择、失败冷却、指数退避；`health(details=True)` 暴露每个 key 的冷却原因。 |
| 安全与访问 | `import_guard.py` | 动态导入白名单，仅允许 `core.` / `integrations.` 前缀。 |
| 运行资源 | `resource_budget.py` | CPU/内存/温度/电源感知，按档位降级。 |
| 公共服务 | `application/` | 防腐层 Facade，为 MCP/CLI 提供稳定核心能力入口。 |
| 公共服务 | `utils.py` | 通用工具：`LazyPath`、文件权限加固、原子写等。 |
| 运维 | `ops/` | health_check、auto_healing、cognitive_readiness、config_audit、evidence_backfill。 |

## 关键数据流

```
Agent 原始会话  →  sync_framework  →  raw_events.db current revision  →  CaptureQueue  →  hephaestus  →  wiki/knowledge_graph
                                               ↓
                              cognitive/observation (exact revision/span terminal)  →  reflection  →  persona
                                               ↓
                                          kia/guard + recap  →  主动推送 / 强制复盘
```

## 新增模块约定

1. 核心域不直接依赖 `integrations.*`；需要适配器能力时通过 `core.application.facade.MnemosServiceFacade` 注入。
2. 敏感数据文件/目录创建后调用 `core.utils.secure_directory/secure_file` 设置 700/600 权限。
3. 公共 API 优先使用 `ConfigProvider` 协议注入配置，保留 `get_config()` 作为默认回退。
4. 新增 `# DEBT(S8): ...` 或 `# DEBT(<id>): ...` 注释标记已知技术债务，并说明原因与后续动作。
