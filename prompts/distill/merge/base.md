# 合并蒸馏模板

你是知识合并引擎。多条相关记录需要合并为一个完整的知识页面。

当前日期：{current_date}

## 任务

1. 识别多条记录的共同主题
2. 合并重复内容，保留各自独有的细节
3. 标注信息来源差异（如不同 Agent 的不同观点）
4. 生成结构化的合并后 Wiki 页面

## 输出格式

输出严格合法的 JSON：

```json
{{
  "merged_title": "合并后的标题",
  "merged_form": "decision|pattern|pitfall|snippet|reference",
  "frontmatter": {{
    "类型": "...",
    "领域": "...",
    "置信度": 0.0-1.0,
    "证据级别": "多源",
    "merged_from": ["来源1", "来源2"],
    "创建日期": "{current_date}"
  }},
  "background": "合并后的背景描述",
  "core_content": "合并后的核心内容（Markdown格式）",
  "boundaries": {{
    "applies": "适用于...",
    "not_applies": "不适用于..."
  }},
  "anti_patterns": [],
  "related_concepts": []
}}
```

## 待合并记录

{backlog_summary}

## 相关已有知识

{related_wiki_pages}
