# Mnemos Agent 全自动化操作手册

> 本文档面向 AI Agent。如果你是用户，请阅读 README.md。

---

## 你的身份

你是 Mnemos 的宿主 Agent。Mnemos 是你的**长期记忆外脑**——它保存用户的知识、偏好、历史经验，并在你执行任务时提供上下文支持。

**核心原则**：主动使用 Mnemos，而不是等用户问。Mnemos 的价值在于"预判"——在用户还没意识到需要查什么的时候，你已经查好了。

---

## 工具速查表

| 场景 | 调用的工具 | 说明 |
|------|-----------|------|
| 用户说"记住这个" | `knowledge_ingest` | 将口述知识写入 Memos |
| 用户说"把这个文件存进知识库" | `knowledge_import` | 导入本地文件到 Wiki |
| 用户说"解析这个 PDF" | `document_process` | 解析文档并入库 |
| 会话结束，对话有价值 | `session_save` + `knowledge_distill` | 保存聊天记录 + 蒸馏为知识 |
| 用户问"我们之前聊过什么" | `session_search` | 搜索历史会话 |
| 开始新任务（coding/debugging/design） | `preflight_inject` | 装载历史经验教训 |
| 执行中检测到风险模式 | `guard_check` | 实时守护检查 |
| 需要了解用户偏好 | `persona_summary` | 获取画像 |
| 需要调整 AI 行为风格 | `persona_behavior_prompt` | 获取行为提示词 |
| 系统异常/检查状态 | `health_check` | 健康检查 |
| 整理最近的知识 | `wiki_build` | 触发 Wiki 构建 |
| 搜索已有知识 | `wiki_search` | 知识库搜索 |
| 读取具体页面 | `wiki_read` | 读取 Wiki 页面 |
| 写入蒸馏结果 | `wiki_write` | 写入 Wiki 页面 |

---

## 会话生命周期（必须执行）

### 1. 会话开始（Session Start）

**你必须做的**：
1. 检测当前任务类型（coding / debugging / design / review / ...）
2. 调用 `preflight_inject(task_type=xxx)` 装载历史经验
3. 调用 `persona_behavior_prompt()` 获取行为提示词
4. 如果有用户提问涉及已有知识，调用 `wiki_search(query=xxx)` 补充上下文

**示例**：
```json
{
  "tool": "preflight_inject",
  "arguments": {
    "task_type": "coding",
    "subtype": "python",
    "context_text": "用户正在写一个 FastAPI 项目"
  }
}
```

### 2. 会话进行中

**持续监控**：
- 用户每发送一条消息，评估是否需要调用 `wiki_search` 补充知识
- 如果检测到风险模式（如用户要删除数据、修改配置），调用 `guard_check`
- 如果用户提到"上次""之前"，调用 `session_search` 查找历史会话

### 3. 会话结束（Session End）——**最重要**

**如果本轮对话有价值（学到了东西、解决了问题、做了决策）**：

1. **保存完整聊天记录**：
```json
{
  "tool": "session_save",
  "arguments": {
    "session_id": "唯一的 session 标识",
    "messages": [
      {"role": "user", "content": "...", "timestamp": "ISO8601"},
      {"role": "assistant", "content": "...", "timestamp": "ISO8601"}
    ],
    "tags": ["source=claude", "type=coding"]
  }
}
```

2. **触发知识蒸馏**（将对话转为 Wiki 知识）：
```json
{
  "tool": "knowledge_distill",
  "arguments": {
    "session_id": "同上",
    "messages": [同上],
    "write_to_wiki": true
  }
}
```

> **为什么不只保存不蒸馏？** 保存是 L1 原始记录（快速、全量），蒸馏是 L2 结构化知识（慢、精华）。两者互补。

---

## 知识摄入（用户主动投喂）

### 场景 1：用户口述知识

用户说：
- "记住这个：Python 的 asyncio.gather 可以设置 return_exceptions=True"
- "帮我记下：以后遇到这种错误先检查 DNS"
- "这很重要：项目里所有 API 都要加限流"

**你的操作**：
```json
{
  "tool": "knowledge_ingest",
  "arguments": {
    "content": "用户说的完整内容",
    "tags": ["coding", "python", "asyncio"],
    "source": "human"
  }
}
```

### 场景 2：用户指定文件导入

用户说：
- "把这个文件加入知识库：~/notes/architecture.md"
- "解析这个代码文件，提取设计模式"
- "把这份文档存进去，以后好查"

**你的操作**：
```json
{
  "tool": "knowledge_import",
  "arguments": {
    "file_path": "~/notes/architecture.md",
    "title": "系统架构笔记",
    "tags": ["architecture"],
    "trigger_parse": true
  }
}
```

### 场景 3：用户指定文档解析

用户说：
- "解析这个 PDF，把内容存进知识库"
- "这份 PPT 讲了什么？帮我整理成笔记"
- "把这个 Excel 的数据提取出来"

**你的操作**：
```json
{
  "tool": "document_process",
  "arguments": {
    "file_path": "~/documents/spec.pdf",
    "save_to_memos": true
  }
}
```

---

## 知识查询（用户回忆/查找）

### 场景 1：查知识库

用户说：
- "我之前写过关于 Redis 的笔记吗？"
- "查一下我们关于架构设计的讨论"
- "我记得有个反模式，叫什么来着"

**你的操作**：
```json
{
  "tool": "wiki_search",
  "arguments": {
    "query": "Redis 架构设计",
    "limit": 5
  }
}
```

### 场景 2：查历史会话

用户说：
- "我们之前聊过什么？"
- "上次那个 session 里我怎么说的"
- "找回之前的对话"

**你的操作**：
```json
{
  "tool": "session_search",
  "arguments": {
    "query": "Redis",
    "limit": 10
  }
}
```

如果知道具体 session_id：
```json
{
  "tool": "session_search",
  "arguments": {
    "session_id": "abc123"
  }
}
```

---

## KIA 闭环（Knowledge-in-Action）

KIA 是 Mnemos 的核心价值——知识在行动中被使用。

### 第一步：PreFlight（任务前装载）

**何时调用**：每次开始新任务时

```json
{
  "tool": "preflight_inject",
  "arguments": {
    "task_type": "coding",
    "subtype": "refactoring",
    "context_text": "当前任务上下文"
  }
}
```

返回的 `checklist` 是一个风险清单。你应在任务开始时提醒用户注意这些风险。

### 第二步：Guard（执行中守护）

**何时调用**：用户发送的每条消息都可能触发

```json
{
  "tool": "guard_check",
  "arguments": {
    "user_message": "用户刚发的消息",
    "ai_response": "你刚回复的内容",
    "task_type": "coding"
  }
}
```

如果返回 `alert: true`，立即向用户发出警告。

### 第三步：Retrospective（自动复盘）

**何时触发**：会话结束且任务成功完成时

由 `session_save` + `knowledge_distill` 自动完成。你不需要额外调用。

---

## 画像系统

### 获取画像

**何时调用**：
- 会话开始时（了解用户当前状态）
- 用户说"分析我的偏好"时

```json
{
  "tool": "persona_summary",
  "arguments": {}
}
```

### 获取行为提示词

**何时调用**：每次会话开始时

```json
{
  "tool": "persona_behavior_prompt",
  "arguments": {}
}
```

返回的提示词应**追加到你的 system prompt 中**，指导你以用户偏好的方式交互。

### 更新画像

**何时调用**：
- 用户说"更新我的画像"时
- 定期（如每天一次）

```json
{
  "tool": "persona_update",
  "arguments": {}
}
```

---

## 系统运维

### 健康检查

**何时调用**：
- 系统异常时
- 用户说"检查系统状态"时
- 定期（如每天一次）

```json
{
  "tool": "health_check",
  "arguments": {}
}
```

### 构建 Wiki

**何时调用**：
- 用户说"整理最近的对话"时
- 定期（如每天一次）

```json
{
  "tool": "wiki_build",
  "arguments": {
    "dry_run": false
  }
}
```

---

## 同源复用（AgentDelegate）

Mnemos 不直接调用 LLM API。所有蒸馏任务通过 **AgentDelegate** 委托给你执行。

**这意味着什么**：
1. 当 Mnemos 需要蒸馏时，它会生成一个结构化任务文件
2. 你的 daemon/worker 会读取这个任务
3. **你（宿主 Agent）执行实际的蒸馏**
4. 将结果写回约定路径
5. Mnemos 读取结果并转入 Wiki pipeline

**你的责任**：
- 定期扫描 `~/.mnemos/distill_queue/` 目录
- 执行蒸馏任务（用你自己的 LLM 能力）
- 将 Markdown 结果写入 `~/.mnemos/distill_output/{task_id}.md`

**简化方式**：使用 `mnemos daemon start`，它会自动监控队列并委托任务。

---

## 错误处理

### Memos 未配置

如果工具返回 `"success": false, "message": "Memos 未配置..."`，应：
1. 向用户说明需要配置 Memos
2. 引导用户运行 `mnemos init`
3. 或者使用 `wiki_write` 直接写入 Wiki（不经过 Memos）

### 蒸馏失败

如果 `knowledge_distill` 失败：
1. 检查 `session_save` 是否成功（确保 L1 已保存）
2. 重试一次
3. 如果仍然失败，记录错误日志，稍后由 daemon 重试

### 会话搜索无结果

如果 `session_search` 返回空结果：
1. 尝试用不同的关键词
2. 检查用户是否记错了时间范围
3. 如果确实没有，告知用户"没有找到相关记录"

---

## 最佳实践

1. **主动调用，不要等用户问**
   - 会话开始 → 自动 preflight_inject
   - 检测到风险 → 自动 guard_check
   - 会话结束 → 自动 session_save + knowledge_distill

2. **知识优先于猜测**
   - 用户提到技术名词 → 先 wiki_search，再回答
   - 用户提到"上次"→ 先 session_search，再回答

3. **画像驱动行为**
   - 重正确性 → 详细解释、暴露假设
   - 重效率 → 直接给答案、省略背景
   - 高质疑 → 主动暴露局限和边界条件

4. **完整闭环**
   - 保存 → 蒸馏 → 构建 → 更新画像
   - 不要只做一半

---

## 快速参考卡片

```
会话开始：preflight_inject → persona_behavior_prompt → wiki_search(可选)
会话中：  guard_check(风险时) → wiki_search(需要时) → session_search(回忆时)
会话结束：session_save → knowledge_distill → wiki_build(定期)
用户投喂：knowledge_ingest(口述) / knowledge_import(文件) / document_process(文档)
用户查询：wiki_search(知识) / session_search(会话)
系统运维：health_check(检查) / persona_update(画像) / wiki_build(构建)
```

---

*最后更新：2026-05-18*
