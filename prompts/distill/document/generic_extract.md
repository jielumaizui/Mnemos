# 通用文档知识提取

你是一位知识管理专家。请从以下文档中提取核心知识点，转化为可复用的结构化知识。

**文档内容：**
{content}

**文档信息：**
- 类型：{judge_category}
- 实体：{judge_entity}

**提取要求：**

输出严格 JSON：
```json
{
  "objective_extraction": {
    "key_achievements": [
      {
        "achievement": "核心知识点/结论",
        "metrics": "支撑信息",
        "factors": "关键条件"
      }
    ],
    "key_challenges": [
      {
        "challenge": "潜在问题/限制",
        "root_cause": "原因分析",
        "lesson": "应对建议"
      }
    ],
    "decisions_made": [
      {
        "decision": "关键判断/选择",
        "outcome": "结果",
        "retrospective": "反思"
      }
    ],
    "patterns_identified": ["发现的模式/规律"],
    "reusable_methods": [
      {
        "method": "可复用方法",
        "context": "适用场景",
        "effectiveness": "有效程度"
      }
    ]
  },
  "relations": [
    {
      "target": "[[相关页面标题]]",
      "type": "prerequisite|related_to|contradicts|derives_from|supercedes",
      "context": "30-100字说明关联场景"
    }
  ],
  "frontmatter": {
    "关键词": ["至少5个核心概念标签"],
    "触发器": ["什么场景下会想起这条知识"],
    "别名": ["其他叫法、简称、同义词"],
    "boundaries": {"applies": "适用于...", "not_applies": "不适用于..."},
    "anti_patterns": ["常见误用、陷阱、错误理解"]
  }
}
```

**规则：**
- relations: 分析与已有知识的关联，无关联则留空数组
- frontmatter: 必须输出，不能省略
- 如果文档内容较浅（如简单参考文档），适当简化输出
