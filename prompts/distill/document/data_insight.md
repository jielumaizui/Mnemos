# 数据洞察提取

你是一位数据分析师。请从以下数据报表中提取关键洞察。

**数据内容：**
{data_content}

{related_pages}

**提取要求：**

输出严格 JSON：
```json
{
  "data_profile": {
    "scope": "数据覆盖范围",
    "time_range": "时间范围",
    "key_metrics": ["指标1", "指标2"]
  },
  "insights": [
    {
      "observation": "观察到的现象/趋势",
      "evidence": "支撑数据（具体数字）",
      "implication": "业务/决策含义",
      "confidence": "高|中|低"
    }
  ],
  "anomalies": [
    {
      "description": "异常描述",
      "data_point": "具体数据",
      "possible_cause": "可能原因"
    }
  ],
  "recommendations": [
    "基于数据的可行动建议"
  ],
  "relations": [
    {
      "target": "[[相关页面标题]]",
      "type": "prerequisite|related_to|contradicts|derives_from|supercedes",
      "context": "30-100字说明这两个知识在什么场景下关联"
    }
  ],
  "frontmatter": {
    "关键词": ["至少5个：核心指标、业务场景、分析方法、关键工具、对立概念"],
    "触发器": ["什么场景下会想起这条知识"],
    "别名": ["其他叫法、简称、同义词"],
    "boundaries": {"applies": "适用于...", "not_applies": "不适用于..."},
    "anti_patterns": ["常见误用、数据陷阱、错误解读方式"]
  }
}
```

**规则：**
- 每个洞察必须有具体数字支撑
- 区分"相关性"和"因果性"
- 标注置信度，不确定的用"低"
- relations: 分析数据与已有知识的关联，无关联则留空数组
- frontmatter: 必须输出，不能省略
