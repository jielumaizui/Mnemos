# 策略提取

你是一位策略分析专家。请客观提取以下方案/计划文档中的内容，不添加你的主观评价。

**文档内容：**
{strategy_content}

{related_pages}

**提取要求：**

输出严格 JSON，包含两个区域：

```json
{
  "objective_extraction": {
    "strategy_overview": {
      "goal": "核心目标",
      "timeframe": "时间框架",
      "target_audience": "目标对象"
    },
    "key_decisions": [
      {
        "decision": "决策内容",
        "rationale": "决策理由",
        "alternatives_considered": "考虑过的替代方案",
        "risks": ["风险1", "风险2"]
      }
    ],
    "action_items": [
      {
        "action": "行动项",
        "owner": "负责人（如有）",
        "deadline": "时间节点（如有）",
        "success_criteria": "成功标准"
      }
    ],
    "methodologies": [
      {
        "name": "使用的通用方法论/框架",
        "how_applied": "如何在本方案中应用"
      }
    ],
    "lessons_learned": [
      "可复用的经验教训"
    ]
  },
  "ai_expansion": {
    "related_concepts": ["相关的通用方法论或理论模型（AI建议）"],
    "potential_blindspots": ["该策略可能忽略的视角或风险（AI提醒）"],
    "practice_suggestions": ["将该方法论应用于其他场景的建议（AI建议）"],
    "critical_questions": ["值得进一步思考的问题（AI提出）"]
  },
  "frontmatter": {
    "关键词": ["至少5个：核心策略、方法论、业务场景、关键工具、风险点"],
    "触发器": ["什么场景下会想起这条策略"],
    "别名": ["其他叫法、简称、同义词"],
    "boundaries": {"applies": "适用于...", "not_applies": "不适用于..."},
    "anti_patterns": ["常见误用、策略陷阱、错误执行方式"]
  }
}
```

**规则：**
- objective_extraction 必须严格基于文档内容，零添加
- 将具体业务动作抽象为通用方法论
- 保留决策逻辑，去掉具体人名/公司名
- ai_expansion 是 AI 关联补充，必须与客观提取分离
- frontmatter: 必须输出，不能省略
