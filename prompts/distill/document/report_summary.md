# 报告复盘提取

你是一位复盘分析专家。请从以下报告/总结中提取关键结论和可复用经验。

**报告内容：**
{report_content}

{related_pages}

**提取要求：**

输出严格 JSON：
```json
{
  "report_meta": {
    "period": "时间周期",
    "scope": "覆盖范围"
  },
  "key_achievements": [
    {
      "achievement": "关键成果",
      "metrics": "支撑数据",
      "factors": "成功因素"
    }
  ],
  "key_challenges": [
    {
      "challenge": "关键挑战",
      "root_cause": "根因分析",
      "lesson": "经验教训"
    }
  ],
  "decisions_made": [
    {
      "decision": "做出的决策",
      "outcome": "结果",
      "retrospective": "复盘：如果重来会怎么做"
    }
  ],
  "patterns_identified": [
    "发现的模式/规律"
  ],
  "reusable_methods": [
    {
      "method": "可复用的方法",
      "context": "适用场景",
      "effectiveness": "有效程度"
    }
  ],
  "relations": [
    {
      "target": "[[相关页面标题]]",
      "type": "prerequisite|related_to|contradicts|derives_from|supercedes",
      "context": "30-100字说明这两个知识在什么场景下关联"
    }
  ],
  "frontmatter": {
    "关键词": ["至少5个：项目类型、核心方法、业务领域、关键成果、风险点"],
    "触发器": ["什么场景下会想起这条复盘"],
    "别名": ["其他叫法、简称、同义词"],
    "boundaries": {"applies": "适用于...", "not_applies": "不适用于..."},
    "anti_patterns": ["常见误用、复盘陷阱、错误归因方式"]
  }
}
```

**规则：**
- relations: 分析与已有知识的关联，无关联则留空数组
- frontmatter: 必须输出，不能省略
