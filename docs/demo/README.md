# Mnemos 惊艳场景 Demo

这是一个可独立运行的端到端故事，展示 Mnemos 如何把一次“踩坑经历”转化为未来任务前的主动提醒。

## 故事

> 用户第一次被 `asyncio.gather` 的 `TimeoutError` 坑到，和 Claude 讨论出解决方案。  
> 一周后，用户又要写一段批量并发调用外部 API 的代码。  
> Mnemos 在任务开始前自动注入之前的经验教训，避免重蹈覆辙。

链路：

```
Claude 对话 JSONL
    ↓
ClaudeSource.parse_turns()
    ↓
SyncEngine.sync_session() → Raw Vault
    ↓
DistillationEngine.process() + write_pages() → Wiki 页面
    ↓
RawIndex.search("asyncio.gather")
    ↓
PreFlightInjector.inject() → 当前任务上下文提醒
```

## 运行

```bash
# 在项目根目录执行
python docs/demo/run_demo.py
```

脚本会在临时目录中创建隔离的 MNEMOS 数据，不会触碰你的 `~/.mnemos`。

## 输出示例

```
=== Mnemos Demo: asyncio.gather pitfall → preflight reminder ===

[1/4] 采集并同步 Claude 对话到 Raw Vault ...
       写入 Wiki 页面: ['/tmp/.../wiki/...']

[2/4] 索引 Raw Vault ...
       搜索 'asyncio.gather' 命中 N 条

[3/4] 模拟一周后遇到相似任务 ...
       Preflight 注入 M 条提醒

✅ Demo 完成：过去的踩坑经验已被预注入到当前任务上下文。
```

## 自定义

- 替换 `docs/demo/fixtures/claude_asyncio_gather.jsonl` 为你自己的对话 JSONL。
- 在 `docs/demo/run_demo.py` 中修改 `_make_fragment()` 以匹配你的领域知识。
- 修改 `_run_preflight()` 的 `context_text` 以测试不同任务场景。
