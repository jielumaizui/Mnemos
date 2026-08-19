# 蒸馏链路常规审计报告

- **审计日期**：2026-06-26
- **审计范围**：Mnemos 蒸馏链路中 LLM 调用相关代码，重点聚焦近期修改：模型名称、调用超时、流式输出
- **审计方式**：人工代码阅读 + 单元测试运行 + 关键路径动态验证

---

## 1. 审计文件清单

| 文件 | 覆盖维度 |
|------|----------|
| `core/hephaestus/distillation_llm.py` | 流式输出、timeout 传递、异常捕获、调用策略 |
| `core/llm_config.py` | 模型名解析、价格查找、限流、chain 解析 |
| `core/config.py` | 默认模型、timeout、chain 配置 |
| `core/hephaestus/distillation_value_judge.py` | LLM 调用入口（L3） |
| `core/hephaestus/distillation_extractor.py` | LLM 调用入口（L4） |
| `core/cli/commands/init.py` | 初始化配置生成 |
| `scripts/auto_setup.py` | 自动安装配置生成 |
| `config/config.example.yaml` | 示例配置 |
| `config/config.example.json` | 示例配置 |
| `tests/unit/test_llm_config.py` | 配置/价格/限流测试 |
| `tests/unit/test_distillation_llm.py` | 路由策略/流式测试 |
| `tests/unit/test_distillation_engine.py` | 引擎成本累加测试 |
| `tests/unit/test_config_contract.py` | 配置契约测试 |

---

## 2. 关键发现与处理

### 2.1 模型名大小写敏感导致价格查找失效（已修复）

**位置**：`core/llm_config.py:58-95`

**问题**：
- `get_provider_price()` 内部将 `model` 统一转小写后再查找
- 但 `DEFAULT_PROVIDER_PRICES` 中 SiliconFlow 的 key 为 `deepseek-ai/DeepSeek-V4-Flash`（保留大写）
- 结果：特定模型价格永远匹配失败，只能 fallback 到 `default`

**代码证据**：
```python
# core/llm_config.py:64-65
provider = (provider or "").lower()
model = (model or "").lower()

# core/llm_config.py:90-94
defaults = DEFAULT_PROVIDER_PRICES.get(provider, {})
if model in defaults:      # "deepseek-ai/deepseek-v4-flash" 不在 defaults 中
    return dict(defaults[model])
if "default" in defaults:
    return dict(defaults["default"])
```

**修复**：
- 新增 `_lookup_price(prices, model)`，对价格表 key 做大小写不敏感匹配
- 同时覆盖用户自定义 `llm.provider_prices` 层
- 位置：`core/llm_config.py:58-113`

**验证**：新增 `tests/unit/test_llm_config.py::TestProviderPrice`，覆盖默认价格和用户自定义价格的大小写匹配

---

### 2.2 流式输出未捕获 OpenAI APIError（已修复）

**位置**：`core/hephaestus/distillation_llm.py:446-521`

**问题**：
- `_try_api_config()` 改为 `stream=True` 后，OpenAI SDK 抛出的 `openai.APIError`、`openai.APITimeoutError`、`openai.RateLimitError` 等异常不在原 except 元组中
- 这些异常会向上穿透，可能导致蒸馏线程崩溃

**代码证据**：
```python
# 修复前
except (
    OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, sqlite3.Error,
    subprocess.SubprocessError
):
```

**修复**：
- 在 except 元组中加入 `openai.APIError`
- 位置：`core/hephaestus/distillation_llm.py:514-521`

**验证**：
- 现有 `tests/unit/test_distillation_llm.py` 全部通过
- 新增 `test_try_api_config_uses_streaming_and_concatenates_chunks` 验证流式拼接

---

### 2.3 `subprocess.TimeoutExpired` 分支可能是遗留代码（观察）

**位置**：`core/hephaestus/distillation_llm.py:419-428`

**说明**：
- `_call_one_config()` 单独捕获 `subprocess.TimeoutExpired`
- 但 OpenAI Python SDK 的 timeout 抛出的是 `openai.APITimeoutError`，已被 `openai.APIError` 基类捕获
- 该分支在新流式实现下几乎不会触发，属于历史遗留

**建议**：可后续清理，保留当前不会导致错误

---

### 2.4 Race 中限流 acquire 可能阻塞 worker（观察）

**位置**：`core/hephaestus/distillation_llm.py:346-383`

**说明**：
- `_race_worker()` 内部调用 `self._rate_limiter.acquire()`，若模型被限流会阻塞等待 60s
- `cancel_event` 不会中断已阻塞在 `acquire()` 中的 worker
- 极端情况下，免费模型 worker 可能因限流阻塞到 race_timeout 结束

**影响**：
- 当前 `race_timeout = 120s`，付费模型通常能先返回，影响可控
- 若付费模型也慢，可能整体超时

**建议**：若未来限流场景频繁，可将 `acquire()` 改为 `can_acquire()` 非阻塞检查，避免 worker 长期阻塞

---

### 2.5 流式 timeout 仍是 total timeout（观察）

**位置**：`core/hephaestus/distillation_llm.py:475-482`

**说明**：
- OpenAI Python SDK 的 `timeout` 参数为 HTTP 请求总超时
- 流式模式下，timeout 仍限制整个 HTTP 连接的最长持续时间（120s），而非 token 间间隔
- 因此流式解决了“模型正在生成但客户端非流式等待导致整体取消”的问题，但不是无时间限制

**影响**：
- 4000 tokens 输出在 120s 内通常足够
- 若模型在 120s 内无法完成完整响应，连接仍会被关闭

---

### 2.6 模型名与 timeout 配置一致性（已确认）

**位置**：
- `core/config.py:464-538`
- `core/llm_config.py:21-28`
- `core/cli/commands/init.py:165-195`
- `scripts/auto_setup.py:540-570`
- `config/config.example.yaml`、`config/config.example.json`

**结论**：
- 默认模型：`siliconflow/deepseek-ai/DeepSeek-V4-Flash` → `dmxapi/kimi-k2.5-free` → `dmxapi/MiniMax-M2.7-free`
- 所有 chain 节点 timeout 统一为 120s
- 示例配置、初始化脚本、代码默认值三者一致
- `core/llm_config.py` 中的 `DMXAPI_MODEL` / `SILICONFLOW_MODEL` 常量与默认配置一致

---

## 3. 测试验证

运行蒸馏链路相关单元测试：

```bash
pytest tests/unit/test_llm_config.py \
       tests/unit/test_distillation_llm.py \
       tests/unit/test_distillation_engine.py \
       tests/unit/test_config_contract.py -q
```

**结果**：
```text
132 passed in 13.37s
```

---

## 4. 结论

- **本次审计共发现并修复 2 个问题**：
  1. 模型名大小写敏感导致价格查找失效
  2. 流式输出未捕获 OpenAI APIError
- **其余 3 项为观察性风险**，当前影响可控，可作为后续技术债处理
- **模型名称、timeout、chain 配置在代码、示例、脚本、文档中保持一致**
- **相关单元测试全部通过**

**总体评估**：蒸馏链路当前状态良好，近期修改（120s timeout 统一、流式输出）在实现上无明显阻塞性缺陷，可继续运行。
