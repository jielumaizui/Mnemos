# -*- coding: utf-8 -*-
"""MCP tool schema declarations for the Agora server."""

from __future__ import annotations

from typing import Callable, Dict


def list_tools(get_tool_category: Callable[[str], str]) -> Dict:
    """列出所有可用 tools（带完整 inputSchema）"""
    tools = [
        {
            "name": "wiki_search",
            "description": (
                "搜索知识库。知识来源包括：1) 用户主动投喂（通过knowledge_ingest存入）"
                "2) L1 storage同步 3) Agent对话蒸馏 4) Retrospective复盘 5) Git历史。"
                "所有知识均经过语义索引、标签构建、热度评分(L0-L9)处理。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "返回数量上限", "default": 5},
                    "session_id": {
                        "type": "string",
                        "description": "调用方当前 session，用于 private scope 过滤",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "调用方项目标识，用于 project scope 过滤",
                        "default": "",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "wiki_read",
            "description": "读取指定 wiki 页面。页面内容经过完整解析器处理：语义索引提取实体/概念/技术栈、自动标签分类、热度评分L0-L9、知识图谱关联。",  # noqa: E501
            "inputSchema": {
                "type": "object",
                "properties": {
                    "page_path": {"type": "string", "description": "wiki 页面相对路径"},
                    "session_id": {
                        "type": "string",
                        "description": "调用方当前 session，用于 private scope 校验",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "调用方项目，用于 project scope 校验",
                        "default": "",
                    },
                },
                "required": ["page_path"],
            },
        },
        {
            "name": "wiki_write",
            "description": "写入 Wiki 页面。Agent 执行蒸馏或生成新知识后，将结果写入 Wiki 知识库。支持 frontmatter 元数据写入。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "page_path": {
                        "type": "string",
                        "description": "wiki 页面相对路径（如 'concepts/my-idea.md'）",
                    },
                    "content": {
                        "type": "string",
                        "description": "页面 Markdown 内容（不含 frontmatter）",
                    },
                    "frontmatter": {
                        "type": "object",
                        "description": "Frontmatter 元数据（可选）",
                        "default": {},
                    },
                    "session_id": {
                        "type": "string",
                        "description": "private scope 必填的当前 session",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "project scope 必填且必须位于服务端 grant",
                        "default": "",
                    },
                },
                "required": ["page_path", "content"],
            },
        },
        {
            "name": "memory_write_project",
            "description": "写入项目级记忆。内容会落到 scopes/project/<project>/ 并写入 scope/project frontmatter。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "页面标题"},
                    "content": {"type": "string", "description": "Markdown 内容"},
                    "project": {
                        "type": "string",
                        "description": "项目名；为空时使用 default",
                        "default": "",
                    },
                    "page_path": {
                        "type": "string",
                        "description": "可选相对路径；会自动置于项目 scope 目录下",
                        "default": "",
                    },
                    "frontmatter": {
                        "type": "object",
                        "description": "额外 frontmatter",
                        "default": {},
                    },
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "memory_write_framework",
            "description": "写入框架级记忆。内容会落到 scopes/framework/<framework>/ 并写入 scope/framework frontmatter。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "页面标题"},
                    "content": {"type": "string", "description": "Markdown 内容"},
                    "framework": {
                        "type": "string",
                        "description": "框架或技术栈名；为空时使用 general",
                        "default": "",
                    },
                    "page_path": {
                        "type": "string",
                        "description": "可选相对路径；会自动置于框架 scope 目录下",
                        "default": "",
                    },
                    "frontmatter": {
                        "type": "object",
                        "description": "额外 frontmatter",
                        "default": {},
                    },
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "memory_write_global",
            "description": "写入全局级记忆。内容会落到 scopes/global/ 并写入 scope/global frontmatter。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "页面标题"},
                    "content": {"type": "string", "description": "Markdown 内容"},
                    "page_path": {
                        "type": "string",
                        "description": "可选相对路径；会自动置于 global scope 目录下",
                        "default": "",
                    },
                    "frontmatter": {
                        "type": "object",
                        "description": "额外 frontmatter",
                        "default": {},
                    },
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "memory_search",
            "description": "按记忆范围搜索 Wiki。scope 可为 all、project、framework 或 global。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "scope": {
                        "type": "string",
                        "description": "搜索范围",
                        "enum": ["all", "project", "framework", "global"],
                        "default": "all",
                    },
                    "limit": {"type": "integer", "description": "返回数量上限", "default": 5},
                    "session_id": {
                        "type": "string",
                        "description": "调用方当前 session，用于 private scope 过滤",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "调用方项目标识，用于 project scope 过滤",
                        "default": "",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "session_search",
            "description": (
                "搜索历史会话记录，自动合并分片内容。支持按关键词或 session_id 查找按 "
                "hash/range/segment 分片存储的完整聊天记录。当用户问'我们之前聊过什么'、"
                "'上次那个session'、'找回之前的对话'时使用此工具。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（支持内容关键词、session_id 片段、hash 前缀等）",
                        "default": "",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "精确 session ID（可选，提供时优先按 session_id 查找）",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回结果数量上限",
                        "default": 10,
                    },
                    "project": {
                        "type": "string",
                        "description": "调用方项目标识，用于 project scope 过滤",
                        "default": "",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "capture_turn",
            "description": "MCP 主动上报单轮对话。只做校验和入队，不直接写 L1 storage，返回 < 200ms。当 Agent 正在与用户对话时，每轮对话结束后调用此工具上报。",  # noqa: E501
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "会话唯一标识"},
                    "turn_id": {
                        "type": "string",
                        "description": "轮次 ID（可选）",
                        "default": "",
                    },
                    "turn_number": {
                        "type": "integer",
                        "description": "轮次序号（可选）",
                        "default": 0,
                    },
                    "user_content": {
                        "type": "string",
                        "description": "用户消息内容（可选）",
                        "default": "",
                    },
                    "assistant_content": {
                        "type": "string",
                        "description": "AI 回复内容（可选）",
                        "default": "",
                    },
                    "timestamp": {
                        "type": "string",
                        "description": "时间戳 ISO 格式（可选）",
                        "default": "",
                    },
                    "model": {
                        "type": "string",
                        "description": "使用的模型名称（可选）",
                        "default": "",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "当前工作目录（可选）",
                        "default": "",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "额外元数据（可选）",
                        "default": {},
                    },
                    "tool_calls": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "本轮工具调用列表（可选）",
                        "default": [],
                    },
                    "tool_results": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "本轮工具结果列表（可选）",
                        "default": [],
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "宿主公开的 reasoning/thinking 摘要或引用（可选，不要求私有思维链）",
                        "default": "",
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "文件、图片、媒体或附件上下文证据（可选）",
                        "default": [],
                    },
                    "raw_event_refs": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "原始事件引用，用于回溯宿主原始记录（可选）",
                        "default": [],
                    },
                    "source_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本轮涉及的源文件路径（可选）",
                        "default": [],
                    },
                    "completeness": {
                        "type": "object",
                        "description": "采集完整性声明，如 source_fidelity、loss_reasons（可选）",
                        "default": {},
                    },
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "session_save",
            "description": "兼容旧入口：保存完整聊天记录到 L1 storage。新接入应优先使用 capture_turn 或 capture_session。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "会话唯一标识"},
                    "messages": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "完整消息列表 [{role, content}]",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表（可选）",
                        "default": [],
                    },
                },
                "required": ["session_id", "messages"],
            },
        },
        {
            "name": "capture_session",
            "description": "MCP 批量上报整个 session 的多轮对话。适用于一次性上报完整对话记录的场景。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "会话唯一标识"},
                    "turns": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "轮次列表 [{turn_number, user_content, assistant_content}]",
                    },
                },
                "required": ["session_id", "turns"],
            },
        },
        {
            "name": "end_session",
            "description": "标记 session 结束。通知 Mnemos 该会话已完成，触发后续处理（如队列排空、会话完整性校验）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "会话唯一标识"},
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "capture_status",
            "description": "查询指定 session/turn 在捕获队列中的状态。用于检查对话是否已成功入队或处理完成。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "会话唯一标识"},
                    "turn_number": {
                        "type": "integer",
                        "description": "轮次序号（可选，-1 表示查询整个 session）",
                        "default": -1,
                    },
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "knowledge_ingest",
            "description": (
                "知识摄入 — 将用户主动提供的人工知识写入L1 storage，自动进入Wiki处理链路"
                "（L1 storage→00-Inbox→语义索引/标签/热度评分→知识图谱）。当用户说'记住这个'、"
                "'帮我记下'、'这很重要'时使用此工具。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "用户提供的知识内容"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表（可选，如 ['coding', 'important']）",
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "knowledge_distill",
            "description": (
                "触发知识蒸馏 — 将原始聊天记录转为结构化 Wiki 知识（问题-解决/决策记录/"
                "经验法则/反模式/方法论/洞察关联 6种形态）。Agent 完成一次有价值的对话后，"
                "应主动调用此工具将对话转为 Wiki 知识。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "会话标识（用于追溯）"},
                    "messages": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "消息列表 [{role, content}]",
                    },
                    "write_to_wiki": {
                        "type": "boolean",
                        "description": "是否直接写入 Wiki",
                        "default": True,
                    },
                },
                "required": ["session_id", "messages"],
            },
        },
        {
            "name": "document_process",
            "description": (
                "处理用户指定路径文档（PDF/PPT/Excel/Word/HTML/EBOOK）。默认按 trusted_user_document "
                "写入 canonical raw，并由 capture outbox 异步进入 Amphora、质量门和 Wiki；"
                "mode=parse 才仅解析预览。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件绝对路径"},
                    "title": {
                        "type": "string",
                        "description": "文档标题（可选）",
                        "default": "",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["parse", "capture", "distill", "watch"],
                        "description": (
                            "导入模式：parse 仅预览，capture 只写 canonical raw，"
                            "distill 写 raw 后由 outbox 异步蒸馏，watch 预检守护监听"
                        ),
                        "default": "distill",
                    },
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "wiki_build",
            "description": (
                "触发 Wiki 构建（L1→L2）。扫描 L1 storage 中的原始记录，对高质量、已完成 "
                "session 执行：质量评分→去重→蒸馏→Wiki页面生成→索引更新→Git提交。当用户说"
                "'整理最近的对话'、'构建Wiki'时使用此工具。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "仅预览不实际写入",
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "knowledge_source_list",
            "description": "列出知识库的来源分布统计（各来源的知识条目数）",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "preflight_inject",
            "description": "KIA闭环-任务前知识装载：优先根据任务类型装载retrospective经验；未命中时自动回退到通用Wiki知识搜索。宿主Agent应在任务开始时调用。",  # noqa: E501
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "description": "任务类型，如 coding、debugging、design",
                    },
                    "subtype": {"type": "string", "description": "子类型", "default": ""},
                    "context_text": {
                        "type": "string",
                        "description": "当前会话上下文，用于场景适配",
                        "default": "",
                    },
                },
                "required": ["task_type"],
            },
        },
        {
            "name": "check_pending_recaps",
            "description": "检查待复盘事项。宿主Agent应在会话开始、任务收尾或回复前调用，用于推动用户复盘。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_context": {
                        "type": "object",
                        "description": "当前用户上下文，如 current_file/task_type，可选",
                        "default": {},
                    },
                    "limit": {"type": "integer", "description": "返回数量上限", "default": 5},
                },
            },
        },
        {
            "name": "recap_start",
            "description": "开始结构化强制复盘，返回三问契约、owner 状态和证据摘要。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "待复盘任务 ID，可来自 check_pending_recaps", "default": ""},
                    "topic": {"type": "string", "description": "无 task_id 时用于创建复盘待办的主题", "default": ""},
                    "mode": {
                        "type": "string",
                        "description": "复盘模式",
                        "enum": ["minimal", "standard", "deep"],
                        "default": "minimal",
                    },
                    "session_id": {"type": "string", "description": "当前会话 ID", "default": ""},
                    "context": {"type": "object", "description": "复盘上下文", "default": {}},
                    "project": {"type": "string", "description": "项目标识", "default": ""},
                    "task_type": {"type": "string", "description": "任务类型", "default": ""},
                    "subtype": {"type": "string", "description": "任务子类型", "default": ""},
                },
            },
        },
        {
            "name": "recap_submit",
            "description": "提交三问答案，生成结构化复盘草稿；缺字段时返回 missing_fields。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recap_id": {"type": "string", "description": "recap_start 返回的复盘 ID"},
                    "answers": {
                        "type": "object",
                        "description": "三问答案，键为 goal_actual/cause_lesson/next_handling",
                        "properties": {
                            "goal_actual": {"type": "string"},
                            "cause_lesson": {"type": "string"},
                            "next_handling": {"type": "string"},
                            "freeform": {
                                "type": "string",
                                "description": "一句话极简回答；系统会自动拆成三问字段",
                            },
                        },
                    },
                    "confirm_level": {
                        "type": "string",
                        "description": "draft 或 user_confirmed",
                        "enum": ["draft", "user_confirmed"],
                        "default": "draft",
                    },
                },
                "required": ["recap_id", "answers"],
            },
        },
        {
            "name": "recap_finalize",
            "description": "用户确认后将结构化复盘写入 06-Retrospectives/复盘，并生成消费计划。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recap_id": {"type": "string", "description": "复盘 ID"},
                    "write_policy": {
                        "type": "string",
                        "description": "写入策略",
                        "default": "save_and_index",
                    },
                    "follow_up_at": {"type": "string", "description": "回看时间 ISO 字符串", "default": ""},
                    "confirmed_by_user": {
                        "type": "boolean",
                        "description": "是否已经获得用户确认",
                        "default": True,
                    },
                },
                "required": ["recap_id"],
            },
        },
        {
            "name": "recap_skip",
            "description": "用户跳过、延后或纠偏本次复盘时记录结构化跳过事件。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recap_id": {"type": "string", "description": "复盘 ID", "default": ""},
                    "task_id": {"type": "string", "description": "待复盘任务 ID", "default": ""},
                    "skip_reason": {
                        "type": "string",
                        "description": "跳过原因",
                        "enum": ["no_time", "low_value", "false_positive", "already_handled", "no_response"],
                    },
                    "user_note": {"type": "string", "description": "用户补充说明", "default": ""},
                },
                "required": ["skip_reason"],
            },
        },
        {
            "name": "recap_feedback",
            "description": "记录用户对复盘结论的准确性、有用性或过期反馈。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recap_id": {"type": "string", "description": "复盘 ID"},
                    "feedback_type": {
                        "type": "string",
                        "description": "反馈类型",
                        "enum": ["accurate", "inaccurate", "useful", "irrelevant", "outdated"],
                    },
                    "comment": {"type": "string", "description": "反馈说明", "default": ""},
                    "supersedes_event_id": {
                        "type": "string",
                        "description": "更改既有反馈时必须精确引用最新反馈事件 ID",
                        "default": "",
                    },
                },
                "required": ["recap_id", "feedback_type"],
            },
        },
        {
            "name": "recap_status",
            "description": "查询复盘状态，防止多 Agent 重复发问或重复写入。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recap_id": {"type": "string", "description": "复盘 ID", "default": ""},
                    "task_id": {"type": "string", "description": "待复盘任务 ID", "default": ""},
                },
            },
        },
        {
            "name": "recap_claim_owner",
            "description": "多 Agent 场景下领取结构化复盘的 owner 锁。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recap_id": {"type": "string", "description": "复盘 ID"},
                    "current_session_id": {"type": "string", "description": "当前会话 ID", "default": ""},
                },
                "required": ["recap_id"],
            },
        },
        {
            "name": "guard_check",
            "description": (
                "KIA闭环-执行中守护检查：检测当前对话是否触及历史经验中的风险点；"
                "分析循环/重复读取告警会返回 threshold_source、threshold_value 和 current_count"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_message": {"type": "string", "description": "用户发送的消息内容"},
                    "ai_response": {
                        "type": "string",
                        "description": "AI 的回复内容（可选）",
                        "default": "",
                    },
                    "task_type": {
                        "type": "string",
                        "description": "任务类型，用于装载对应守护清单",
                        "default": "",
                    },
                    "subtype": {"type": "string", "description": "子类型", "default": ""},
                    "context": {
                        "type": "object",
                        "description": (
                            "可选执行上下文，如 current_file/current_tool/current_command/"
                            "tool_calls/recent_tool_calls/edited_files，用于检测重复读取和分析循环"
                        ),
                        "default": {},
                    },
                },
                "required": ["user_message"],
            },
        },
        {
            "name": "persona_summary",
            "description": "获取当前会话范围内、已授权的用户画像摘要",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "请求收窄到的会话 ID；画像对象必须精确匹配",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "请求收窄到的项目范围",
                        "default": "",
                    },
                },
            },
        },
        {
            "name": "persona_behavior_prompt",
            "description": "获取当前会话范围内、已授权画像驱动的 AI 行为提示词",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "请求收窄到的会话 ID；画像对象必须精确匹配",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "请求收窄到的项目范围",
                        "default": "",
                    },
                },
            },
        },
        {
            "name": "persona_behavior_metrics",
            "description": "获取当前会话范围内、已授权画像消费指标",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "统计最近多少天（默认 30）",
                        "default": 30,
                    },
                    "session_id": {
                        "type": "string",
                        "description": "请求收窄到的会话 ID；画像对象必须精确匹配",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "请求收窄到的项目范围",
                        "default": "",
                    },
                },
            },
        },
        {
            "name": "persona_record_explicit_evidence",
            "description": "仅将精确 canonical Raw 用户原话写入 Persona v2。请求必须选择 source_authority_id，并提供包含同一精确 span 的 source_messages；assistant、tool、外部材料一律拒绝。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "object",
                                "properties": {
                                    "source_revision_id": {"type": "string"},
                                },
                                "required": ["source_revision_id"],
                            },
                            "source_authority_id": {"type": "string"},
                            "signal_type": {"type": "string"},
                            "dimension": {"type": "string"},
                            "quote": {"type": "string"},
                            "confidence": {"type": "number", "default": 0.5},
                            "assertion_id": {"type": "string", "default": ""},
                            "expected_revision_id": {"type": "string", "default": ""},
                            "session_id": {"type": "string", "default": ""},
                            "project": {"type": "string", "default": ""},
                        },
                        "required": [
                            "source",
                            "source_authority_id",
                            "signal_type",
                            "dimension",
                            "quote",
                        ],
                    },
                    "source_messages": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "与 source_revision_id 精确绑定的 role-local Raw span",
                    },
                },
                "required": ["request", "source_messages"],
            },
        },
        {
            "name": "persona_update",
            "description": "触发用户画像更新。采集最新信号并重新计算三层画像（能量/认知/价值）。当用户说'更新我的画像'、'重新分析我的偏好'时使用此工具。",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "signal_collect",
            "description": "触发信号采集（从各数据源收集用户行为信号）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "指定采集哪些源（如 session, git, l1_storage），默认按配置",
                    },
                },
            },
        },
        {
            "name": "retrospective_list",
            "description": "列出可用的 retrospective 经验",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "description": "按任务类型过滤",
                        "default": None,
                    },
                    "limit": {"type": "integer", "description": "返回数量上限", "default": 10},
                    "session_id": {
                        "type": "string",
                        "description": "当前 session，用于 private scope 校验",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "当前项目，用于 project scope 校验",
                        "default": "",
                    },
                },
            },
        },
        {
            "name": "health_check",
            "description": (
                "系统健康摘要。返回稳定的组件状态与 health_check_ids_hash，适合宿主运行验收；"
                "完整本地诊断请运行 python3 mnemos_cli.py health --json。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "agent_runtime_probe",
            "description": (
                "Agent Kit 运行能力验收。宿主先调用 health_check 获取 "
                "health_check_ids_hash，再提交固定 synthetic-safe 样本；服务端只持久化"
                "完整性与时间回执，不保存样本文本。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "health_check_ids_hash": {
                        "type": "string",
                        "description": "同一 MCP 连接刚取得的 canonical health check hash",
                    },
                    "sample": {
                        "type": "object",
                        "description": "mnemos.agent_runtime_probe.v1 固定安全样本",
                        "properties": {
                            "schema_version": {
                                "type": "string",
                                "const": "mnemos.agent_runtime_probe.v1",
                            },
                            "user_content": {
                                "type": "string",
                                "const": "mnemos-runtime-probe-user",
                            },
                            "assistant_content": {
                                "type": "string",
                                "const": "mnemos-runtime-probe-assistant",
                            },
                            "tool_calls": {"type": "array"},
                            "tool_results": {"type": "array"},
                            "completeness": {"type": "object"},
                        },
                        "required": [
                            "schema_version",
                            "user_content",
                            "assistant_content",
                            "tool_calls",
                            "tool_results",
                            "completeness",
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": ["health_check_ids_hash", "sample"],
                "additionalProperties": False,
            },
        },
        {
            "name": "build_cognitive_state",
            "description": "读取 ACL 过滤后的 canonical CognitiveStateSnapshot。该工具只读；不可用状态会返回 typed not_initialized，不会初始化数据库。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "object",
                        "description": "可选状态过滤条件（object_type/object_id/scope_type/scope_id）",
                        "default": {},
                    },
                    "session_id": {"type": "string", "default": ""},
                    "project": {"type": "string", "default": ""},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "record_decision",
            "description": "用精确 canonical Raw span 建立 SourceAuthorityCatalog 后，原子封存 material decision。缺失或不匹配来源一律拒绝。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "trace": {"type": "object", "description": "mnemos DecisionTrace 输入"},
                    "source_messages": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "与 trace.source.source_revision_id 精确绑定的 role-local Raw spans",
                        "default": [],
                    },
                },
                "required": ["trace"],
                "additionalProperties": False,
            },
        },
        {
            "name": "apply_outcome",
            "description": "用一个精确 tool-observation Raw span 为已打开 prediction 记录 outcome；来源、scope 与窗口不匹配时拒绝。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "feedback": {"type": "object", "description": "mnemos outcome 输入"},
                    "source_messages": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "与 feedback.source.source_revision_id 精确绑定的 role-local Raw spans",
                        "default": [],
                    },
                },
                "required": ["feedback"],
                "additionalProperties": False,
            },
        },
        {
            "name": "self_diagnose",
            "description": (
                "Mnemos 自诊断 — 返回完整的系统状态报告，包括：已连接的 Agent、数据源状态、"
                "L1 storage/Wiki 连接状态、缺失的配置项。宿主 Agent 应在每次会话开始时调用此工具，"
                "了解当前连接状态并决定下一步操作。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "configure_wiki",
            "description": (
                "配置 Wiki/Obsidian 路径 — 设置知识库根目录。当用户的 Obsidian Vault 路径与"
                "当前配置不一致时使用此工具。配置后会持久化到 ~/.mnemos/configs/main.json。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "vault_path": {
                        "type": "string",
                        "description": "Wiki 知识库根目录绝对路径，应与运行时配置中的 Mnemos Vault 对齐",
                    },
                },
                "required": ["vault_path"],
            },
        },
        {
            "name": "detect_sources",
            "description": (
                "数据源状态检测 — 返回所有 Agent 数据源和外部系统的连接状态。包括：各 Agent "
                "数据目录是否存在、hooks 是否生效、L1 storage 是否连通、Wiki 目录是否可写。"
                "宿主 Agent 应在启动时调用此工具自检。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "context_aware_search",
            "description": (
                "上下文感知搜索 — 知识图谱召回 + 画像加权评分。相比 wiki_search，增加了用户"
                "画像加权（领域偏好、形态偏好、技术栈、时间模式），返回更精准的排序结果。"
                "当需要更精准的知识检索时使用此工具。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索查询"},
                    "limit": {"type": "integer", "description": "最大结果数", "default": 10},
                    "working_dir": {
                        "type": "string",
                        "description": "当前工作目录（用于上下文感知）",
                        "default": "",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "调用方当前 session，用于 private scope 过滤",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "调用方项目标识，用于 project scope 过滤",
                        "default": "",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "intent_route",
            "description": (
                "意图路由 — 规则匹配（不调 LLM），5 种意图分类：recall(回忆上下文→L1 storage)、"
                "ignore_push(忽略推送)、knowledge(知识查询→Wiki)、task(任务执行→直接执行)、"
                "chat(闲聊→直接回复)。优先级：纠正表>时间词>忽略推送>疑问词>动作词>默认。"
                "当返回 needs_correction=true 时，建议向用户确认真实意图并调用 intent_correct 写入纠正。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_input": {"type": "string", "description": "用户输入文本"},
                    "working_dir": {
                        "type": "string",
                        "description": "当前工作目录（用于上下文感知）",
                        "default": "",
                    },
                },
                "required": ["user_input"],
            },
        },
        {
            "name": "intent_correct",
            "description": "意图纠正 — 记录用户/宿主 Agent 确认后的真实意图。后续对相同或相似输入调用 intent_route 时将优先返回纠正后的意图。",  # noqa: E501
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_input": {"type": "string", "description": "用户输入文本"},
                    "original_intent": {
                        "type": "string",
                        "description": "intent_route 返回的原始意图",
                    },
                    "corrected_intent": {
                        "type": "string",
                        "description": "纠正后的意图：recall / ignore_push / knowledge / task / chat",
                    },
                },
                "required": ["user_input", "original_intent", "corrected_intent"],
            },
        },
        {
            "name": "blindspot_check",
            "description": (
                "盲点检查 — 搜索时检测知识空白。当用户搜索某个主题但知识库中缺少相关记录时"
                "返回盲点提醒。24小时冷却，每天最多1条即时提醒。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询（检测是否为知识空白）",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "predictive_push",
            "description": (
                "预测性知识推送 — 基于统一提醒引擎的上下文推送。当检测到用户可能需要某知识时"
                "主动推送。返回结果包含 topic，展示后应调用 push_feedback 记录用户接受/忽略/取消。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_input": {
                        "type": "string",
                        "description": "用户输入文本（用于信号检测）",
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "当前工作目录（作为 recent_context 传入提醒引擎）",
                        "default": "",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "当前 session，用于 private scope 校验",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "当前项目，用于 project scope 校验",
                        "default": "",
                    },
                },
                "required": ["user_input"],
            },
        },
        {
            "name": "delivery_display_ack",
            "description": "仅在宿主实际渲染受准 predictive delivery 后写入不可变展示回执；不把路由成功伪装成展示成功。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "delivery_event_id": {"type": "string"},
                    "rendered_content_hash": {
                        "type": "string",
                        "description": "宿主已展示内容的 sha256:... 摘要；不上传正文",
                    },
                },
                "required": ["delivery_event_id", "rendered_content_hash"],
                "additionalProperties": False,
            },
        },
        {
            "name": "push_feedback",
            "description": "推送反馈 — 强绑定 predictive delivery event，写入 canonical reaction/attribution；不直接更新冷却、信任、评分或 outcome。inaccurate/outdated 必须提供最新 reaction event、精确纠错目标和理由。",  # noqa: E501
            "inputSchema": {
                "type": "object",
                "properties": {
                    "delivery_event_id": {
                        "type": "string",
                        "description": "predictive_push 返回的 delivery_event_id；反馈只绑定这一条实际投递",
                    },
                    "topic": {
                        "type": "string",
                        "description": "推送主题（predictive_push 返回的 topic）",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["accept", "ignore", "dismiss", "inaccurate", "outdated"],
                        "description": (
                            "反馈动作：accept / ignore / dismiss / inaccurate / outdated"
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "predictive_push 使用的 session scope",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "predictive_push 使用的 project scope",
                        "default": "",
                    },
                    "supersedes_event_id": {
                        "type": "string",
                        "description": "inaccurate/outdated 必填：该投递当前最新 canonical feedback_event_id",
                        "default": "",
                    },
                    "correction_target_ref": {
                        "type": "string",
                        "description": "inaccurate/outdated 必填：被纠正内容的精确 target/span ref",
                        "default": "",
                    },
                    "correction_reason": {
                        "type": "string",
                        "description": "inaccurate/outdated 必填：明确的纠错理由",
                        "default": "",
                    },
                },
                "required": ["delivery_event_id", "topic", "action"],
            },
        },
        {
            "name": "freshness_check",
            "description": "知识新鲜度检查 — 检查特定实体的知识是否过时。版本绑定知识与最新版本对比，上下文知识90天未更新标记过期。搜索附加型，不主动弹出。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string", "description": "实体名称"},
                    "session_id": {
                        "type": "string",
                        "description": "当前 session，用于 private scope 校验",
                        "default": "",
                    },
                    "project": {
                        "type": "string",
                        "description": "当前项目，用于 project scope 校验",
                        "default": "",
                    },
                },
                "required": ["entity_name"],
            },
        },
        # L3/L4/L5 Reflection 运行时工具
        {
            "name": "observation_run",
            "description": "运行 Observation Engine（L3）。全量或增量提取 L1 raw + L2 wiki 中的客观观察，持久化到 Observation Index。",  # noqa: E501
            "inputSchema": {
                "type": "object",
                "properties": {
                    "full": {
                        "type": "boolean",
                        "description": "是否全量重新提取",
                        "default": False,
                    },
                    "since": {
                        "type": "string",
                        "description": "增量模式：ISO 格式时间戳（如 2026-06-01T00:00:00）",
                        "default": "",
                    },
                },
            },
        },
        {
            "name": "observation_search",
            "description": "搜索 Observation Index（L3），按维度、来源类型筛选最近观察。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": ("维度筛选（如 attention/growth/decision）"),
                        "default": "",
                    },
                    "source_type": {
                        "type": "string",
                        "description": "来源类型筛选（raw/wiki/aggregate）",
                        "default": "",
                    },
                    "limit": {"type": "integer", "description": "返回数量上限", "default": 20},
                },
            },
        },
        {
            "name": "reflect_on_input",
            "description": (
                "基于用户输入自动触发 Reflection（L4）。默认自动调用 LLM 生成洞察摘要与关键发现，"
                "返回 prompt_used / insight_summary / key_points / llm_called / llm_error。"
                "将 auto_llm 设为 false 时只返回 prompt_used，由宿主 Agent 自行调用 LLM。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "用户输入文本"},
                    "auto_llm": {
                        "type": "boolean",
                        "description": "是否由 Mnemos 自动调用 LLM 生成洞察（默认 true）",
                        "default": True,
                    },
                    "session_id": {"type": "string", "description": "当前会话范围", "default": ""},
                    "project": {"type": "string", "description": "当前项目范围", "default": ""},
                },
                "required": ["text"],
            },
        },
        {
            "name": "reflect_manually",
            "description": (
                "手动触发一次通用 Reflection（L4）。默认自动调用 LLM 生成洞察摘要与关键发现，"
                "返回 prompt_used / insight_summary / key_points / llm_called / llm_error。"
                "将 auto_llm 设为 false 时只返回 prompt_used，由宿主 Agent 自行调用 LLM。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Reflection 查询，默认使用配置项 reflection.manual_query",
                        "default": "",
                    },
                    "auto_llm": {
                        "type": "boolean",
                        "description": "是否由 Mnemos 自动调用 LLM 生成洞察（默认 true）",
                        "default": True,
                    },
                    "session_id": {"type": "string", "description": "当前会话范围", "default": ""},
                    "project": {"type": "string", "description": "当前项目范围", "default": ""},
                },
            },
        },
        {
            "name": "reflection_feedback",
            "description": "对指定 Reflection 提交用户反馈（L5）。反馈类型：accurate / inaccurate / insightful / irrelevant。",  # noqa: E501
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reflection_id": {"type": "string", "description": "Reflection 记录 ID"},
                    "feedback_type": {
                        "type": "string",
                        "description": "反馈类型：accurate / inaccurate / insightful / irrelevant",
                    },
                    "comment": {"type": "string", "description": "可选评论", "default": ""},
                    "supersedes_event_id": {
                        "type": "string",
                        "description": "更改既有反馈时必须精确引用最新 canonical feedback event",
                        "default": "",
                    },
                    "correction_target_ref": {
                        "type": "string",
                        "description": "inaccurate 纠正所绑定的精确 Reflection target ref",
                        "default": "",
                    },
                    "correction_reason": {
                        "type": "string",
                        "description": "inaccurate 纠正原因",
                        "default": "",
                    },
                    "session_id": {"type": "string", "description": "当前会话范围", "default": ""},
                    "project": {"type": "string", "description": "当前项目范围", "default": ""},
                },
                "required": ["reflection_id", "feedback_type"],
            },
        },
        {
            "name": "reflection_pending",
            "description": "获取等待用户反馈的 Reflection 列表（L5）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "hours_since": {
                        "type": "number",
                        "description": "多少小时以来的 Reflection",
                        "default": 24,
                    },
                    "limit": {"type": "integer", "description": "返回数量上限", "default": 20},
                    "session_id": {"type": "string", "description": "当前会话范围", "default": ""},
                    "project": {"type": "string", "description": "当前项目范围", "default": ""},
                },
            },
        },
    ]
    _CATEGORY_LABELS = {
        "core": "【核心】",
        "extended": "【扩展】",
        "auxiliary": "【辅助】",
        "lifecycle": "【生命周期】",
        "advanced": "【高级】",
    }
    for tool in tools:
        category = get_tool_category(tool["name"])  # type: ignore[arg-type]
        label = _CATEGORY_LABELS.get(category, "【高级】")
        tool["description"] = f"{label} {tool['description']}"
        tool["annotations"] = {
            "title": f"{tool['name']} ({category})",
        }
    return {"tools": tools}
