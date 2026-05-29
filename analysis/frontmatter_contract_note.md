# Frontmatter 契约记录

## 需求

Obsidian 中展示给用户看的 Markdown frontmatter 优先使用中文字段，贴合实际阅读和管理场景。

## 实现原则

- Obsidian 页面使用中文字段，例如：`类型`、`名称`、`领域`、`摘要`、`状态`、`知识阶段`、`来源数量`、`证据级别`。
- Python 内部、数据库、事件、JSON 仍保留英文 canonical key，例如：`type`、`name`、`domain`、`summary`、`status`、`knowledge_stage`、`source_count`、`evidence_level`。
- 代码层通过统一映射读写 frontmatter，避免中英字段散落在各模块中。
- 后续测试应按中文展示契约验收，同时验证内部映射不丢字段。
