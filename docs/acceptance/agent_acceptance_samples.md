# Agent Acceptance Samples

状态：第 1 步验收产物。

本清单定义 Mnemos 在进入 raw 入库、蒸馏、检索审计前使用的统一样本矩阵。机器可校验入口是 `tests/fixtures/agent_acceptance_samples/manifest.json`，契约实现是 `core/agent_kit/acceptance_contracts.py`。

## 覆盖 Agent

| Agent | 样本引用前缀 | 必需能力 |
|---|---|---|
| codex | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/codex/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity`, `reasoning`, `attachments` |
| claude | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/claude/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity`, `reasoning` |
| hermes | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/hermes/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity`, `reasoning` |
| opencode | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/opencode/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity`, `reasoning` |
| openclaw | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/openclaw/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity` |
| crush | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/crush/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity`, `attachments` |
| kiro | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/kiro/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity`, `reasoning`, `attachments` |
| kimi | `tests/fixtures/agent_acceptance_samples/manifest.json#/agents/kimi/samples/...` | `visible_text`, `tool_calls`, `tool_results`, `source_fidelity`, `reasoning`, `attachments` |

## 标准样本类型

| 样本类型 | 用途 | 预期输出 |
|---|---|---|
| `ordinary_qa` | 普通问答，只含用户消息和助手回复 | 通过 `raw_event_contract.v1` 和 `distilled_knowledge_contract.v1` |
| `tool_call` | 工具调用和工具结果 | `tool_calls`、`tool_results` 保留结构化内容 |
| `long_multiturn` | 多轮长会话 | turn 顺序稳定，长文本不被静默截断 |
| `file_attachment_context` | 文件、附件、媒体或上下文引用 | 能采集则写入 `attachments`，不能采集必须有降级声明 |
| `artifact_uri_context` | 工具结果、附件、截图、终端输出、测试报告等 artifact 引用 | 能采集则写入标准 `artifact_refs`，URI 使用 `mnemos-artifact://...`，不能采集必须有降级声明 |
| `reasoning_metadata` | reasoning/thinking metadata | 只保存宿主暴露的摘要、metadata 或引用，不要求私有思维链 |
| `cross_directory_project` | 跨目录或项目会话 | `canonical_session_id`、`session_aliases`、`working_dir`、`dedupe_strategy` 可判定 |
| `interrupted_error` | 错误或中断会话 | 半截数据可入库，`source_fidelity`/`loss_reasons` 明确，不静默丢弃 |

## 重复验收命令

```bash
python3 scripts/verify_acceptance_contracts.py
pytest tests/unit/test_acceptance_contracts.py
```

通过时必须满足：

- 8 个目标 Agent 全部出现在样本矩阵中。
- 该 8 个 host 分母由 `core/agent_kit/agent_source_support_manifest.json` 派生；Aider、Gemini、Cursor、Windsurf 是 ingestion-only，不得加入本样本矩阵或 host full-power 分母。
- 每个 Agent 都包含 7 类标准样本引用。
- 每个样本声明 `raw_event_contract.v1` 和 `distilled_knowledge_contract.v1` 作为预期输出。
- 每个目标 Agent 的内置 `AgentSource.completeness_capabilities()` 声明 `source_fidelity=full`，且包含 Agent Kit 要求的认知证据能力。
