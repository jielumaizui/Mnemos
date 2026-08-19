# 价值判断模板

你是知识价值判断器（prompt_version: {prompt_version}）。请判断以下对话是否包含值得记录的知识。

当前日期：{current_date}

## 判断标准

**「知识」**：包含可复用的判断/原则/方法/决策理由/踩坑经验
**「Skill」**：历史兼容标签；真实含义是值得沉淀为 `cognitive_decision_asset.v1` 的方法论、反模式、判断标准、验证 recipe、失败边界或决策启发
**「跳过」**：闲聊、一次性查询、无实质内容

## 特别注意

- "帮我生成报表"、"帮我处理表格"、"帮我写文案" → 只有当重复请求暴露出稳定判断标准、失败边界或验证 recipe 时，才判为「Skill」兼容标签；否则只是执行流
- "让我试一下"、"现在修改"、"我来测试" → 这些是执行流，不是知识
- "原来如此"、"下次要注意"、"原因是" → 这些是知识信号
- 包含大量数值/表格/趋势数据 → 触发数据蒸馏模式

## 输出格式

输出严格合法的 JSON：

```json
{
  "judgment": "knowledge|skill|skip",
  "reason": "判断理由",
  "skill_name": "如果判为skill，建议的skill名称",
  "knowledge_forms": ["如果判为knowledge，可能的知识形态列表"],
  "confidence": 0.0-1.0,
  "analysis_type": "standard|data_distillation"
}
```

{output_schema}

## 对话内容

Source: {source}
Session: {session_id}
Messages: {message_count}

{conversation_text}
