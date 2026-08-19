# Agent 行为规范（强制）

**本规范适用于所有支持 Mnemos 的 Agent：Claude Code、Codex、Hermes、OpenClaw、OpenCode、Kimi Code。**

违反本规范的 Agent 会导致知识碎片化、无法追溯、无法复用，Mnemos 的品质闭环（硬校验 → 入库/失败分流 → 知识图谱构建）将完全失效。

---

## 文件蒸馏触发规则

**当用户给出文件路径并要求"蒸馏/导入知识库"时，Agent 必须调用 `document_process` MCP tool。**

Agent **禁止**自行读取文件、自行调用 API 蒸馏、直接展示结果。

正确路径：
```
用户: "解析这个PDF"
Agent → 调用 document_process(file_path, write_to_wiki=True)
Mnemos → 解析文件 → LLM API 蒸馏 → 硬校验 → 入库 Wiki
Agent → 向用户报告结果 + 提供 Wiki 链接
```

错误路径（禁止）：
```
用户: "解析这个PDF"
Agent → 自己读文件 → 自己调API → 直接展示结果
      → Mnemos 完全不知情，硬校验/知识图谱/Wiki入库全部失效
```

**原因**：只有 Mnemos 管道能保证品质闭环（硬校验 → 入库/失败分流 → 知识图谱构建）。Agent 自行处理会导致知识碎片化、无法追溯、无法复用。

---

## 禁止行为（仅限蒸馏/导入场景）

**以下禁令仅在用户要求"蒸馏/导入知识库"时生效。** 正常的代码编辑、审查、调试等场景，Agent 可自行读取文件。

1. **用户要求蒸馏/导入知识库时，禁止自行读取文件**进行解析/蒸馏/总结 — 必须走 `document_process`
2. **禁止自行调用 LLM API** 对文件内容进行蒸馏 — Mnemos 直接调用 LLM API
3. **禁止直接展示未经 Mnemos 管道处理的蒸馏结果** — 必须入库后方可引用

---

## 正确行为

| 用户请求 | Agent 动作 |
|---------|-----------|
| "蒸馏这个 PDF" | 调用 `document_process(file_path, write_to_wiki=True)` |
| "把这个文件加入知识库" | 调用 `document_process(file_path, write_to_wiki=True)` |
| "解析这份文档" | 调用 `document_process(file_path, write_to_wiki=True)` |
| "总结这个 PPT" | 调用 `document_process(file_path, write_to_wiki=True)` |

---

## 品质闭环说明

Mnemos 直接调用 LLM API 完成所有蒸馏任务，不委托给宿主 Agent。

**为什么 Agent 不能自行蒸馏**：
1. **品质不可控**：Agent 可能绕过 Mnemos 管道，自行处理文件，导致硬校验、知识图谱、Wiki 入库全部失效
2. **约定不可靠**：Agent 的自主行为无法强制约束，君子协定在复杂场景下必然被违反
3. **流程闭环**：只有 Mnemos 自己执行，才能保证从原始素材 → 蒸馏 → 硬校验 → 入库 → 知识图谱的完整闭环
