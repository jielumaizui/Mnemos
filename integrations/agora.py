"""
MCP Server - Model Context Protocol 服务器

职责：
- 通过 stdin/stdout 与 AI Agent 通信
- 暴露 mnemos 的能力作为 MCP tools
- 支持：知识库查询、用户画像读取、信号采集触发、KIA 闭环（预加载、守护）

协议：MCP (Model Context Protocol) over JSON-RPC 2.0
传输：stdio

Note: 本模块只将已声明的协议、I/O、配置和持久化失败转换为 JSON-RPC
错误；AssertionError 等未知程序错误保持可见，避免协议层掩盖实现缺陷。
"""

from __future__ import annotations

# Agora — 古希腊广场 — MCP 协议中心，公共交流场所
# 原模块: mcp_server.py


import json
import sys
import logging
import sqlite3
from typing import Dict, List, Optional, Any, Tuple

from core.access_policy import (
    AccessNarrowing,
    MCP_TOOL_POLICIES,
    PrincipalEnvelope,
    authorize_tool_call,
)
from core.agent_kit.authorization import (
    AgentAuthorizationStore,
    MCPLaunchCredentialStore,
)
from core.application.facade import DefaultMnemosServiceFacade, MnemosServiceFacade
from core.runtime_environment import environment_get
from datetime import datetime, timedelta
from integrations.agora_tools import schema as _schema_tools
from integrations.agora_contract import (  # noqa: F401
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    MCP_LAUNCH_CAPABILITY_REF_ENV,
    MCP_RECOVERABLE_ERRORS,
    MCP_TOOL_EXECUTION_ERROR,
    _compact_mcp_health_report,
)

# 配置日志到 stderr，避免污染 stdout（MCP 协议通道）
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, stream=sys.stderr, format="%(asctime)s [mcp] %(levelname)s: %(message)s"
)


class MCPServer:
    """MCP 服务器 - stdio 模式，JSON-RPC 2.0"""

    def __init__(
        self,
        facade: MnemosServiceFacade | None = None,
        *,
        launch_credential: str = "",
        authorization_store: AgentAuthorizationStore | None = None,
    ):
        self._facade = facade or DefaultMnemosServiceFacade(logger)
        store = authorization_store
        if store is None and launch_credential:
            store = AgentAuthorizationStore(initialize=False)
        self._authorization_store = store
        self._launch_credential = launch_credential
        self._principal: PrincipalEnvelope | None = None
        try:
            self._principal = (
                store.resolve_mcp_principal(launch_credential)
                if store is not None and launch_credential
                else None
            )
        except (OSError, ValueError, sqlite3.Error):
            logger.warning("MCP launch capability store unavailable", exc_info=True)
            self._principal = None
        self.tools = self._register_tools()
        if set(self.tools) != set(MCP_TOOL_POLICIES):
            missing = sorted(set(self.tools) - set(MCP_TOOL_POLICIES))
            stale = sorted(set(MCP_TOOL_POLICIES) - set(self.tools))
            raise RuntimeError(
                "tool policy registry mismatch: "
                f"unregistered_handlers={missing}, stale_policies={stale}"
            )
        advertised_tools = _schema_tools.list_tools(self._get_tool_category)["tools"]
        advertised_names = {str(tool.get("name", "")) for tool in advertised_tools}
        if advertised_names != set(self.tools):
            missing = sorted(set(self.tools) - advertised_names)
            stale = sorted(advertised_names - set(self.tools))
            raise RuntimeError(
                "tool schema registry mismatch: "
                f"missing_schemas={missing}, stale_schemas={stale}"
            )
        self._tool_input_properties = {
            str(tool["name"]): frozenset(
                str(key) for key in tool.get("inputSchema", {}).get("properties", {})
            )
            for tool in advertised_tools
        }
        kia_service = getattr(self._facade, "_kia", None)
        # guard_check 会话缓存：guard_key -> (InProcessGuard, created_at)
        self._guard_sessions: Dict[str, Tuple[Any, datetime]] = getattr(
            kia_service,
            "_guard_sessions",
            {},
        )
        self._guard_session_ttl_seconds: int = getattr(
            kia_service,
            "_guard_session_ttl_seconds",
            86400,
        )
        self._guard_sessions_max: int = getattr(kia_service, "_guard_sessions_max", 1000)
        # guard_check 知识缓存：(task_type, subtype) -> (knowledge, loaded_at)
        self._guard_knowledge_cache: Dict[str, Tuple[Any, datetime]] = getattr(
            kia_service,
            "_guard_knowledge_cache",
            {},
        )
        self._guard_cache_ttl_seconds: int = getattr(
            kia_service,
            "_guard_cache_ttl_seconds",
            300,
        )
        self._guard_knowledge_cache_max: int = getattr(
            kia_service,
            "_guard_knowledge_cache_max",
            256,
        )

    def _prune_guard_knowledge_cache(self):
        """清理过期的 guard_check 知识缓存，并限制最大容量。"""
        now = datetime.now()
        ttl = timedelta(seconds=self._guard_cache_ttl_seconds)
        expired = [
            k for k, (_, loaded_at) in self._guard_knowledge_cache.items() if now - loaded_at >= ttl
        ]
        for k in expired:
            del self._guard_knowledge_cache[k]
        while len(self._guard_knowledge_cache) > self._guard_knowledge_cache_max:
            self._guard_knowledge_cache.popitem(last=False)

    def _prune_guard_sessions(self):
        """清理过期的 guard_check 会话缓存，并限制最大容量。"""
        now = datetime.now()
        ttl = timedelta(seconds=self._guard_session_ttl_seconds)
        expired = []
        for k, (guard, created_at) in self._guard_sessions.items():
            if now - created_at >= ttl:
                expired.append(k)
                try:
                    if hasattr(guard, "close"):
                        guard.close()
                except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                    logger.debug("关闭过期 guard session 失败", exc_info=True)
        for k in expired:
            del self._guard_sessions[k]
        while len(self._guard_sessions) > self._guard_sessions_max:
            _, (guard, _) = self._guard_sessions.popitem(last=False)
            try:
                if hasattr(guard, "close"):
                    guard.close()
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logger.debug("关闭超限 guard session 失败", exc_info=True)

    def _register_tools(self) -> Dict[str, Any]:
        """注册可用 tools"""
        return {
            "wiki_search": self._tool_wiki_search,
            "wiki_read": self._tool_wiki_read,
            "wiki_write": self._tool_wiki_write,
            "session_search": self._tool_session_search,
            "capture_turn": self._tool_capture_turn,
            "capture_session": self._tool_capture_session,
            "end_session": self._tool_end_session,
            "capture_status": self._tool_capture_status,
            "session_save": self._tool_session_save,
            "knowledge_ingest": self._tool_knowledge_ingest,
            "knowledge_distill": self._tool_knowledge_distill,
            "document_process": self._tool_document_process,
            "wiki_build": self._tool_wiki_build,
            "memory_write_project": self._tool_memory_write_project,
            "memory_write_framework": self._tool_memory_write_framework,
            "memory_write_global": self._tool_memory_write_global,
            "memory_search": self._tool_memory_search,
            "preflight_inject": self._tool_preflight_inject,
            "guard_check": self._tool_guard_check,
            "persona_summary": self._tool_persona_summary,
            "persona_behavior_prompt": self._tool_persona_behavior_prompt,
            "persona_behavior_metrics": self._tool_persona_behavior_metrics,
            "persona_record_explicit_evidence": self._tool_persona_record_explicit_evidence,
            "persona_update": self._tool_persona_update,
            "signal_collect": self._tool_signal_collect,
            "retrospective_list": self._tool_retrospective_list,
            "check_pending_recaps": self._tool_check_pending_recaps,
            "recap_start": self._tool_recap_start,
            "recap_submit": self._tool_recap_submit,
            "recap_finalize": self._tool_recap_finalize,
            "recap_skip": self._tool_recap_skip,
            "recap_feedback": self._tool_recap_feedback,
            "recap_status": self._tool_recap_status,
            "recap_claim_owner": self._tool_recap_claim_owner,
            "knowledge_source_list": self._tool_knowledge_source_list,
            "health_check": self._tool_health_check,
            "agent_runtime_probe": self._tool_agent_runtime_probe,
            "self_diagnose": self._tool_self_diagnose,
            "configure_wiki": self._tool_configure_wiki,
            "detect_sources": self._tool_detect_sources,
            "context_aware_search": self._tool_context_aware_search,
            "build_cognitive_state": self._tool_build_cognitive_state,
            "record_decision": self._tool_record_decision,
            "apply_outcome": self._tool_apply_outcome,
            "intent_route": self._tool_intent_route,
            "intent_correct": self._tool_intent_correct,
            "blindspot_check": self._tool_blindspot_check,
            "predictive_push": self._tool_predictive_push,
            "delivery_display_ack": self._tool_delivery_display_ack,
            "push_feedback": self._tool_push_feedback,
            "freshness_check": self._tool_freshness_check,
            # L3/L4/L5 Reflection 运行时工具
            "observation_run": self._tool_observation_run,
            "observation_search": self._tool_observation_search,
            "reflect_on_input": self._tool_reflect_on_input,
            "reflect_manually": self._tool_reflect_manually,
            "reflection_feedback": self._tool_reflection_feedback,
            "reflection_pending": self._tool_reflection_pending,
        }

    # 工具分类：帮助 Agent 优先选择核心工具，降低上下文负担
    _TOOL_CATEGORIES: Dict[str, List[str]] = {
        "core": [
            "preflight_inject",  # 会话开始时装载知识
            "guard_check",  # 执行中守护
            "wiki_search",  # 搜索知识库
            "wiki_read",  # 读取知识页面
            "document_process",  # 文件蒸馏唯一入口
        ],
        "extended": [
            "knowledge_ingest",  # 知识摄入
            "knowledge_distill",  # 触发蒸馏
            "wiki_build",  # 构建 Wiki
            "memory_write_project",  # 写入项目级记忆
            "memory_write_framework",  # 写入框架级记忆
            "memory_write_global",  # 写入全局级记忆
            "memory_search",  # 按记忆范围搜索
            "session_search",  # 搜索历史会话
            "persona_summary",  # 获取画像
            "persona_update",  # 更新画像
            "persona_behavior_prompt",  # 画像行为提示
            "persona_behavior_metrics",  # 画像行为提示指标
            "persona_record_explicit_evidence",  # 精确用户原话写入画像
            "check_pending_recaps",  # 检查待复盘
            "retrospective_list",  # 列出复盘经验
            "recap_start",  # 开始结构化复盘
            "recap_submit",  # 提交三问答案
            "recap_finalize",  # 确认入库
            "recap_skip",  # 记录跳过原因
            "recap_feedback",  # 记录复盘反馈
            "recap_status",  # 查询复盘状态
            "recap_claim_owner",  # 领取 owner 锁
        ],
        "auxiliary": [
            "health_check",  # 健康检查
            "agent_runtime_probe",  # Agent Kit 运行能力验收
            "self_diagnose",  # 自诊断
            "detect_sources",  # 数据源检测
            "configure_wiki",  # 配置 Wiki 路径
            "knowledge_source_list",  # 知识来源统计
            "signal_collect",  # 信号采集
            "context_aware_search",  # 上下文感知搜索（wiki_search 的增强版）
            "build_cognitive_state",  # 认知状态只读视图
        ],
        "lifecycle": [
            "capture_turn",  # 单轮上报（通常由 hooks 自动触发）
            "capture_session",  # 批量上报（通常由 hooks 自动触发）
            "end_session",  # 标记 session 结束（通常由 hooks 自动触发）
            "capture_status",  # 查询捕获状态
        ],
        "advanced": [
            "intent_route",  # 意图路由
            "intent_correct",  # 意图纠正
            "blindspot_check",  # 盲区检查
            "predictive_push",  # 预测推送
            "delivery_display_ack",  # 宿主实际展示后的回执
            "push_feedback",  # 推送反馈
            "record_decision",  # 认知决策记录
            "apply_outcome",  # 认知结果记录
            "freshness_check",  # 新鲜度检查
            "wiki_write",  # 直接写 Wiki（建议优先使用 document_process/knowledge_distill）
            "reflect_on_input",  # 对输入触发 Reflection
            "reflect_manually",  # 手动触发 Reflection
            "reflection_feedback",  # 提交 Reflection 反馈
            "reflection_pending",  # 查看待反馈 Reflection
            "observation_run",  # 运行 Observation Engine
            "observation_search",  # 搜索 Observation Index
        ],
    }

    @classmethod
    def _get_tool_category(cls, tool_name: str) -> str:
        """根据工具名返回分类，未匹配返回 advanced。"""
        for category, names in cls._TOOL_CATEGORIES.items():
            if tool_name in names:
                return category
        return "advanced"

    def _server_principal(self) -> PrincipalEnvelope:
        """Return the startup-bound principal after request authorization."""
        if self._principal is None:
            raise RuntimeError("principal_required")
        return self._principal

    # ---- 辅助方法 ----

    # ---- Tool 实现 ----

    def _tool_wiki_search(
        self,
        query: str,
        limit: int = 5,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """搜索知识库

        统一搜索入口：优先 ContextAwareSearch（KG 召回 + 正文搜索 + 画像加权），
        无结果时回退到 WikiReader（标题/实体/概念/路径索引）。
        """
        results, access_summary = self._facade.wiki_search(
            query,
            limit=limit,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(
                session_id=session_id,
                project=project,
            ),
        )

        return {
            "success": True,
            "results": results,
            "query": query,
            "access_filter": access_summary,
        }

    def _infer_type_from_path(self, page_path: str) -> str:
        """从路径推断知识类型"""
        return self._facade.infer_type_from_path(page_path)

    def _scope_slug(self, value: str) -> str:
        """Return a filesystem-friendly ASCII slug for scope pages."""
        return self._facade.scope_slug(value)

    def _scope_page_path(
        self,
        scope: str,
        title: str,
        page_path: str = "",
        scope_name: str = "",
    ) -> str:
        return self._facade.scope_page_path(scope, title, page_path, scope_name)

    def _tool_memory_write_project(
        self,
        title: str,
        content: str,
        project: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
    ) -> Dict:
        """写入项目级记忆。"""
        return self._facade.memory_write_project(
            title,
            content,
            project=project,
            page_path=page_path,
            frontmatter=frontmatter,
            principal=self._server_principal(),
        )

    def _tool_memory_write_framework(
        self,
        title: str,
        content: str,
        framework: str = "",
        page_path: str = "",
        frontmatter: Dict | None = None,
    ) -> Dict:
        """写入框架级记忆。"""
        return self._facade.memory_write_framework(
            title,
            content,
            framework=framework,
            page_path=page_path,
            frontmatter=frontmatter,
            principal=self._server_principal(),
        )

    def _tool_memory_write_global(
        self,
        title: str,
        content: str,
        page_path: str = "",
        frontmatter: Dict | None = None,
    ) -> Dict:
        """写入全局级记忆。"""
        return self._facade.memory_write_global(
            title,
            content,
            page_path=page_path,
            frontmatter=frontmatter,
            principal=self._server_principal(),
        )

    def _tool_memory_search(
        self,
        query: str,
        scope: str = "all",
        limit: int = 5,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """按三层记忆范围搜索 Wiki。"""
        return self._facade.memory_search(
            query,
            scope=scope,
            limit=limit,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_wiki_read(
        self,
        page_path: str,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """读取指定 wiki 页面"""
        return self._facade.wiki_read(
            page_path,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_session_search(
        self,
        query: str = "",
        session_id: str = "",
        uid: str = "",
        limit: int = 10,
        days: Optional[int] = None,
        source: Optional[str] = None,
        project: str = "",
    ) -> Dict:
        """
        搜索历史会话记录

        使用场景：
        - 用户说"我们之前聊过什么"
        - 需要查找某次 session 的完整对话
        - 通过 uid 反查原始对话
        - 按时间范围过滤（如最近 7 天）
        - 按 Agent 来源过滤（如只搜 Hermes 的对话）

        返回结构化片段（匹配行前后上下文），而非整文件内容。
        """
        return self._facade.session_search(
            query=query,
            session_id=session_id,
            uid=uid,
            limit=limit,
            days=days,
            source=source,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_knowledge_ingest(
        self,
        content: str,
        tags: List[str] | None = None,
    ) -> Dict:
        """
        知识摄入 — 将用户主动提供的人工知识写入 StorageBackend，进入知识库处理链路。

        完整流程：
        1. Agent 调用此工具，把用户口头/输入的知识存入 StorageBackend
        2. 同步机制将内容同步到 Wiki 的 00-Inbox/
        3. Charon（Connect Worker）对内容进行语义索引、实体提取、标签构建、热度评分
        4. 知识正式纳入图谱，可被 wiki_search / wiki_read 检索

        这是除自动同步、Agent 对话蒸馏、Git 历史提取之外，
        另一个重要的知识入口：用户主动投喂。
        """
        return self._facade.knowledge_ingest(
            content,
            tags=tags,
            principal=self._server_principal(),
        )

    def _tool_preflight_inject(
        self, task_type: str, subtype: str = "", context_text: str = ""
    ) -> Dict:
        """
        KIA 闭环 - 任务前知识装载

        根据任务类型从 retrospective 经验库装载历史教训和检查清单。
        这是 KIA（Knowledge-in-Action）闭环的第一步。
        """
        return self._facade.preflight_inject(
            task_type,
            subtype,
            context_text,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(),
        )

    def _tool_check_pending_recaps(self, user_context: Dict | None = None, limit: int = 5) -> Dict:
        """检查待复盘事项，供宿主 Agent 在回复前进行轻提醒或强提醒。"""
        return self._facade.check_pending_recaps(user_context=user_context, limit=limit)

    def _tool_recap_start(
        self,
        task_id: str = "",
        topic: str = "",
        mode: str = "minimal",
        session_id: str = "",
        context: Dict | None = None,
        project: str = "",
        task_type: str = "",
        subtype: str = "",
    ) -> Dict:
        """开始结构化复盘，返回三问契约和状态。"""
        return self._facade.recap_start(
            task_id=task_id,
            topic=topic,
            mode=mode,
            source_agent=self._server_principal().agent,
            owner_agent=self._server_principal().agent,
            source_agents=[self._server_principal().agent],
            session_id=session_id,
            context=context,
            project=project,
            task_type=task_type,
            subtype=subtype,
        )

    def _tool_recap_submit(
        self,
        recap_id: str,
        answers: Dict,
        confirm_level: str = "draft",
    ) -> Dict:
        """提交三问答案并生成结构化草稿。"""
        return self._facade.recap_submit(
            recap_id=recap_id,
            answers=answers,
            confirm_level=confirm_level,
            source_agent=self._server_principal().agent,
        )

    def _tool_recap_finalize(
        self,
        recap_id: str,
        write_policy: str = "save_and_index",
        follow_up_at: str = "",
        confirmed_by_user: bool = True,
    ) -> Dict:
        """确认复盘草稿并正式写入 Wiki。"""
        return self._facade.recap_finalize(
            recap_id=recap_id,
            write_policy=write_policy,
            follow_up_at=follow_up_at,
            confirmed_by_user=confirmed_by_user,
            source_agent=self._server_principal().agent,
        )

    def _tool_recap_skip(
        self,
        recap_id: str = "",
        task_id: str = "",
        skip_reason: str = "",
        user_note: str = "",
    ) -> Dict:
        """记录用户跳过、延后或纠偏复盘的结构化事件。"""
        return self._facade.recap_skip(
            recap_id=recap_id,
            task_id=task_id,
            skip_reason=skip_reason,
            user_note=user_note,
            owner_agent=self._server_principal().agent,
            source_agent=self._server_principal().agent,
        )

    def _tool_recap_feedback(
        self,
        recap_id: str,
        feedback_type: str,
        comment: str = "",
        supersedes_event_id: str = "",
    ) -> Dict:
        """记录用户对复盘结论的反馈。"""
        return self._facade.recap_feedback(
            recap_id=recap_id,
            feedback_type=feedback_type,
            comment=comment,
            source_agent=self._server_principal().agent,
            supersedes_event_id=supersedes_event_id,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(),
        )

    def _tool_recap_status(self, recap_id: str = "", task_id: str = "") -> Dict:
        """查询复盘状态。"""
        return self._facade.recap_status(
            recap_id=recap_id,
            task_id=task_id,
            source_agent=self._server_principal().agent,
        )

    def _tool_recap_claim_owner(
        self,
        recap_id: str,
        current_session_id: str = "",
    ) -> Dict:
        """领取结构化复盘 owner 锁。"""
        return self._facade.recap_claim_owner(
            recap_id=recap_id,
            owner_agent=self._server_principal().agent,
            current_session_id=current_session_id,
        )

    def _tool_guard_check(
        self,
        user_message: str,
        ai_response: str = "",
        task_type: str = "",
        subtype: str = "",
        context: Dict | None = None,
    ) -> Dict:
        """
        KIA 闭环 - 执行中守护检查

        检测当前对话是否触及历史经验中的风险点。
        需要先调用 preflight_inject 装载知识（会自动复用）。
        这是 KIA 闭环的第二步。
        """
        return self._facade.guard_check(
            user_message=user_message,
            ai_response=ai_response,
            task_type=task_type,
            subtype=subtype,
            context=context,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(),
        )

    def _tool_persona_summary(self, session_id: str = "", project: str = "") -> Dict:
        """获取用户画像摘要"""
        return self._facade.persona_summary(
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_persona_behavior_prompt(
        self,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """获取画像驱动的 AI 行为提示词（含 Mnemos 连接指南）"""
        return self._facade.persona_behavior_prompt(
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_persona_behavior_metrics(
        self,
        days: int = 30,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """获取画像行为提示最近 N 天的使用指标。"""
        return self._facade.persona_behavior_metrics(
            days=days,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_persona_record_explicit_evidence(
        self,
        request: Dict,
        source_messages: List[Dict] | None = None,
    ) -> Dict:
        """Record one Profile v2 fact from an exact canonical user Raw span."""

        try:
            catalog = self._cognitive_source_authority_catalog(request, source_messages)
        except ValueError as exc:
            return self._cognitive_contract_failure("source_authority_invalid", exc)
        session_id = str(request.get("session_id") or "")
        project = str(request.get("project") or "")
        return self._facade.record_explicit_profile_evidence(
            request,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
            source_authority_catalog=catalog,
        )

    def _load_onboarding_prompt(self) -> str:
        """加载宿主 Agent 连接指南（根据当前系统状态动态裁剪）"""
        return self._facade.load_onboarding_prompt()

    def _tool_signal_collect(self, sources: List[str] | None = None) -> Dict:
        """触发信号采集"""
        return self._facade.signal_collect(sources=sources)

    def _tool_retrospective_list(
        self,
        task_type: str | None = None,
        limit: int = 10,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """列出可用的 retrospective 经验"""
        return self._facade.retrospective_list(
            task_type,
            limit,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_knowledge_source_list(self) -> Dict:
        """列出知识库的来源分布统计"""
        return self._facade.knowledge_source_list()

    def _tool_wiki_write(
        self,
        page_path: str,
        content: str,
        frontmatter: Dict | None = None,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """
        写入 Wiki 页面

        使用场景：
        - Agent 执行蒸馏后，将结果写入 Wiki
        - Agent 生成新的知识页面
        - 更新已有页面的内容
        """
        return self._facade.wiki_write(
            page_path,
            content,
            frontmatter,
            principal=self._server_principal(),
            session_id=session_id,
            project=project,
        )

    def _tool_session_save(
        self,
        session_id: str,
        messages: List[Dict],
        tags: List[str] | None = None,
    ) -> Dict:
        """
        保存完整聊天记录到 L1 storage（原始池）

        ⚠️ Deprecated: 请使用 capture_turn / capture_session。
        此工具现已统一走 CaptureService，不再直接调用后端客户端。
        """
        return self._facade.session_save(
            session_id,
            messages,
            tags,
            self._server_principal().agent,
        )

    def _tool_capture_turn(
        self,
        session_id: str,
        turn_id: str = "",
        turn_number: int = 0,
        user_content: str = "",
        assistant_content: str = "",
        timestamp: str = "",
        model: str = "",
        cwd: str = "",
        metadata: Dict | None = None,
        tool_calls: list | None = None,
        tool_results: list | None = None,
        reasoning: str = "",
        attachments: list | None = None,
        raw_event_refs: list | None = None,
        source_files: list | None = None,
        completeness: Dict | None = None,
    ) -> Dict:
        """
        MCP 主动上报单轮对话。

        只做校验和入队，不直接写 L1 storage。
        返回 < 200ms。
        """
        return self._facade.capture_turn(
            source_agent=self._server_principal().agent,
            session_id=session_id,
            turn_id=turn_id,
            turn_number=turn_number,
            user_content=user_content,
            assistant_content=assistant_content,
            timestamp=timestamp,
            model=model,
            cwd=cwd,
            metadata=metadata,
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning=reasoning,
            attachments=attachments,
            raw_event_refs=raw_event_refs,
            source_files=source_files,
            completeness=completeness,
        )

    def _tool_capture_session(
        self,
        session_id: str,
        turns: List[Dict],
    ) -> Dict:
        """
        MCP 批量上报整个 session。
        """
        return self._facade.capture_session(
            self._server_principal().agent,
            session_id,
            turns,
        )

    def _tool_end_session(
        self,
        session_id: str,
    ) -> Dict:
        """
        标记 session 结束。
        """
        return self._facade.end_session(self._server_principal().agent, session_id)

    def _tool_capture_status(
        self,
        session_id: str,
        turn_number: int = -1,
    ) -> Dict:
        """
        查询指定 session/turn 的队列状态。
        """
        return self._facade.capture_status(
            self._server_principal().agent,
            session_id,
            turn_number,
        )

    def _tool_knowledge_distill(
        self, session_id: str, messages: List[Dict], write_to_wiki: bool = True
    ) -> Dict:
        """
        触发知识蒸馏（入队 → HephaestusWorker → 直接调用 LLM API）

        将原始聊天记录入蒸馏队列，由后台 HephaestusWorker 消费，并通过配置的
        OpenAI-compatible API 执行蒸馏。宿主 Agent 只负责上报/触发，不替代
        Mnemos 执行蒸馏思考。

        使用场景：
        - Agent 完成一次有价值的对话后，主动触发蒸馏
        - 将技术讨论、调试过程、设计决策转为 Wiki 知识
        """
        return self._facade.knowledge_distill(
            session_id,
            messages,
            write_to_wiki,
            principal=self._server_principal(),
        )

    def _tool_document_process(
        self,
        file_path: str,
        title: str = "",
        mode: str = "distill",
    ) -> Dict:
        """
        处理文档文件（PDF/PPT/Excel/Word/HTML/EBOOK）。

        【Agent 强制规范】当用户给出文件路径并要求"蒸馏/导入知识库"时，
        Agent 必须调用本 tool。此场景下禁止自行读取文件并蒸馏、禁止自行调用 API、
        禁止直接展示未经管道的处理结果。只有 Mnemos 管道能保证品质闭环
        （硬校验 → 入库/失败分流 → 知识图谱构建）。正常的代码编辑/审查/调试，Agent 可自行读取文件。

        - mode=distill（默认）: 写 canonical raw，capture outbox 异步进入质量门与 Wiki
        - mode=capture: 仅写 canonical raw，由 raw projection 生成可读投影
        - mode=parse: 仅解析文档，返回结构、摘要和预览

        使用场景：
        - 用户说"解析这个 PDF"
        - 用户说"把这份 PPT 的内容存到知识库"
        - 提取文档结构、大纲、关键内容
        """
        return self._facade.document_process(
            file_path,
            title,
            mode=mode,
            principal=self._server_principal(),
        )

    def _tool_wiki_build(self, dry_run: bool = False) -> Dict:
        """
        触发 Wiki 回追构建（L1 → L2，catch-up 模式）

        扫描 L1 storage 中未进入 distill_queue 的已完成 session，批量蒸馏。
        这是**回追工具**，用于处理因 daemon 未运行等原因遗漏的 L1 记录；
        正常生产路径应通过 HephaestusWorker 消费 distill_queue。

        执行步骤：
        1. 质量评分
        2. 内容去重
        3. 知识蒸馏（七层流水线）
        4. Wiki 页面生成
        5. 索引更新
        6. Git 自动提交

        使用场景：
        - 手动补漏："把最近没处理的对话整理成 Wiki"
        - 定时回追任务（非主路径）
        """
        return self._facade.wiki_build(dry_run=dry_run)

    def _tool_persona_update(self) -> Dict:
        """
        触发用户画像更新

        采集最新信号并重新计算三层画像（能量/认知/价值）。
        使用场景：
        - 用户说"更新我的画像"
        - 定期画像刷新（daemon 每小时触发）
        """
        return self._facade.persona_update(principal=self._server_principal())

    def _tool_context_aware_search(
        self,
        query: str,
        limit: int = 10,
        working_dir: str = "",
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """
        上下文感知搜索 — 知识图谱召回 + 画像加权评分

        相比 wiki_search，增加了用户画像加权（领域偏好、形态偏好、技术栈、时间模式），
        返回更精准的排序结果。
        """
        return self._facade.context_aware_search(
            query,
            limit=limit,
            working_dir=working_dir,
            session_id=session_id,
            project=project,
            principal=self._server_principal(),
        )

    def _tool_intent_route(self, user_input: str, working_dir: str = "") -> Dict:
        """
        意图路由 — 规则匹配优先，低置信/歧义时 LLM 兜底

        返回意图类型和数据源建议：
        - recall: 原话/证据/历史会话 → session_search
        - mixed_recall: 上次如何解决 → session_search + context_aware_search
        - system_status: 系统状态 → health_check / doctor / status
        - persona: 用户画像 → persona 工具
        - recap: 复盘/提醒 → check_pending_recaps
        - ignore_push: 忽略/拒绝推送 → 不推送
        - knowledge: 知识查询 → context_aware_search / wiki_search
        - task: 任务执行 → 直接执行
        - chat: 闲聊/其他 → 直接回复

        当 needs_correction 为 True 时，建议宿主 Agent 向用户确认真实意图，
        并调用 intent_correct 写入纠正记录。
        当 llm_fallback 为 True 时，表示规则置信度较低，已由 LLM 兜底分类。
        """
        return self._facade.intent_route(user_input, working_dir)

    def _tool_intent_correct(
        self, user_input: str, original_intent: str, corrected_intent: str
    ) -> Dict:
        """
        意图纠正 — 记录用户/宿主 Agent 确认后的真实意图。

        后续对相同或相似输入调用 intent_route 时，将优先返回纠正后的意图。
        """
        return self._facade.intent_correct(user_input, original_intent, corrected_intent)

    def _tool_blindspot_check(self, query: str, session_id: str = "") -> Dict:
        """
        盲点检查 — 搜索时检测知识空白

        当用户搜索某个主题但知识库中缺少相关记录时，返回盲点提醒。
        同一 topic 在同一 session 内只提醒一次；用户忽略后 7 天冷却。
        """
        return self._facade.blindspot_check(
            query,
            session_id=session_id,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id),
        )

    def _tool_predictive_push(
        self,
        user_input: str,
        working_dir: str = "",
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """
        预测性知识推送 — 基于统一提醒引擎的上下文推送。

        当检测到用户可能需要某知识时主动推送。返回结果包含 topic，
        宿主 Agent 应在展示后调用 push_feedback 记录用户接受/忽略/取消。

        通过 ACL 的推送会记录到 push_history。
        """
        return self._facade.predictive_push(
            user_input,
            working_dir=working_dir,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_delivery_display_ack(
        self,
        delivery_event_id: str,
        rendered_content_hash: str,
    ) -> Dict:
        """Record a presentation receipt after this host has rendered a delivery."""
        return self._facade.record_delivery_display(
            delivery_event_id,
            rendered_content_hash,
            principal=self._server_principal(),
        )

    def _tool_build_cognitive_state(
        self,
        context: Dict | None = None,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        return self._facade.build_cognitive_state(
            context or {},
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    @staticmethod
    def _cognitive_source_authority_catalog(
        request: Dict,
        source_messages: List[Dict] | None,
    ):
        from core.evidence.source_authority import SourceAuthorityCatalog

        source = request.get("source") if isinstance(request, dict) else None
        if not isinstance(source, dict):
            raise ValueError("cognitive request source must be an object")
        source_revision_id = str(source.get("source_revision_id") or "").strip()
        if not source_revision_id:
            raise ValueError("cognitive request source_revision_id is required")
        catalog = SourceAuthorityCatalog.from_messages(
            source_messages or (),
            allowed_source_event_ids=(source_revision_id,),
        )
        catalog.require_admissible()
        if any(entry.span_status != "exact" for entry in catalog.entries):
            raise ValueError(
                "cognitive contract requires exact role-local Raw source spans"
            )
        return catalog

    @staticmethod
    def _cognitive_contract_failure(error_code: str, error: Exception) -> Dict:
        return {
            "success": False,
            "schema_version": "mnemos.cognitive_operation_failure.v1",
            "status": "rejected",
            "error_code": error_code,
            "message": str(error),
        }

    def _tool_record_decision(
        self,
        trace: Dict,
        source_messages: List[Dict] | None = None,
    ) -> Dict:
        try:
            catalog = self._cognitive_source_authority_catalog(trace, source_messages)
        except ValueError as exc:
            return self._cognitive_contract_failure("source_authority_invalid", exc)
        return self._facade.record_decision(
            trace,
            principal=self._server_principal(),
            source_authority_catalog=catalog,
        )

    def _tool_apply_outcome(
        self,
        feedback: Dict,
        source_messages: List[Dict] | None = None,
    ) -> Dict:
        try:
            catalog = self._cognitive_source_authority_catalog(feedback, source_messages)
        except ValueError as exc:
            return self._cognitive_contract_failure("source_authority_invalid", exc)
        return self._facade.apply_outcome(
            feedback,
            principal=self._server_principal(),
            source_authority_catalog=catalog,
        )

    def _tool_push_feedback(
        self,
        topic: str,
        action: str,
        delivery_event_id: str,
        session_id: str = "",
        project: str = "",
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        """
        推送反馈 — 记录 canonical reaction/attribution，不直接更新下游状态。

        action 可选：
        accept / ignore / dismiss 单次只记录；不等于有用、成功或持久偏好。
        inaccurate / outdated 还必须传入最新 supersedes_event_id、精确
        correction_target_ref 与 correction_reason，随后只生成 gated proposal。
        """
        return self._facade.push_feedback(
            topic,
            action,
            delivery_event_id,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
            supersedes_event_id=supersedes_event_id,
            correction_target_ref=correction_target_ref,
            correction_reason=correction_reason,
        )

    def _tool_freshness_check(
        self,
        entity_name: str,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """
        知识新鲜度检查 — 版本绑定 + 上下文过期

        检查特定实体的知识是否过时：
        - 版本绑定知识：与最新版本对比
        - 上下文知识：90 天未更新则标记过期

        搜索附加型：只在搜索时展示，不主动弹出。MCP 入口固定为纯读；
        自动刷新必须通过单独的写权限工作流触发。
        """
        return self._facade.freshness_check(
            entity_name,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    # ---- L3/L4/L5 Reflection 运行时工具 ----

    def _tool_observation_run(self, full: bool = False, since: str = "") -> Dict:
        """
        运行 Observation Engine（L3）

        - full=True: 全量重新提取 L1 + L2
        - full=False 且 since 有效: 增量提取 since 之后的新内容
        - 默认: 全量提取
        """
        return self._facade.observation_run(full=full, since=since)

    def _tool_observation_search(
        self,
        dimension: str = "",
        source_type: str = "",
        limit: int = 20,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """搜索 Observation Index（L3）。"""
        return self._facade.observation_search(
            dimension=dimension,
            source_type=source_type,
            limit=limit,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _get_reflection_engine(self, use_llm: bool = True) -> Any:
        """构造已注册 Layer 5 消费者的 ReflectionEngine，确保 MCP 入口闭环。"""
        return self._facade.get_reflection_engine(use_llm=use_llm)

    def _tool_reflect_on_input(
        self,
        text: str,
        auto_llm: bool = True,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """
        基于用户输入自动触发 Reflection（L4）

        默认自动调用 LLM 生成洞察摘要与关键发现，返回完整结果与 LLM 调用状态。
        将 auto_llm 设为 false 时只返回 prompt_used，由宿主 Agent 自行调用 LLM。
        """
        return self._facade.reflect_on_input(
            text,
            auto_llm=auto_llm,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_reflect_manually(
        self,
        query: str = "",
        auto_llm: bool = True,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """
        手动触发一次通用 Reflection（L4）

        默认自动调用 LLM 生成洞察摘要与关键发现，返回完整结果与 LLM 调用状态。
        将 auto_llm 设为 false 时只返回 prompt_used，由宿主 Agent 自行调用 LLM。
        """
        return self._facade.reflect_manually(
            query,
            auto_llm=auto_llm,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_reflection_feedback(
        self,
        reflection_id: str,
        feedback_type: str,
        comment: str = "",
        session_id: str = "",
        project: str = "",
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        """对指定 Reflection 提交用户反馈（L5）。"""
        return self._facade.reflection_feedback(
            reflection_id,
            feedback_type,
            comment,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
            supersedes_event_id=supersedes_event_id,
            correction_target_ref=correction_target_ref,
            correction_reason=correction_reason,
        )

    def _tool_reflection_pending(
        self,
        hours_since: float = 24,
        limit: int = 20,
        session_id: str = "",
        project: str = "",
    ) -> Dict:
        """获取等待用户反馈的 Reflection 列表（L5）。"""
        return self._facade.reflection_pending(
            hours_since=hours_since,
            limit=limit,
            principal=self._server_principal(),
            narrowing=AccessNarrowing(session_id=session_id, project=project),
        )

    def _tool_health_check(self) -> Dict:
        """
        系统健康检查

        检查 Mnemos 各组件状态：
        - 配置完整性
        - StorageBackend 连通性
        - Wiki 目录可写性
        - 各模块可导入性
        - 最近构建/蒸馏状态
        """
        report = self._facade.health_check()
        health_hash = str(report.get("health_check_ids_hash") or "")
        if self._principal is not None and health_hash:
            self._facade.agent_health_observed(self._principal.agent, health_hash)
        return _compact_mcp_health_report(report)

    def _tool_agent_runtime_probe(
        self,
        health_check_ids_hash: str,
        sample: Dict,
    ) -> Dict:
        """Record a content-free receipt for an authorized host MCP roundtrip."""
        return self._facade.agent_runtime_probe(
            self._server_principal().agent,
            health_check_ids_hash,
            sample,
        )

    def _tool_self_diagnose(self) -> Dict:
        """Mnemos 自诊断 — 返回完整系统状态报告"""
        return self._facade.self_diagnose()

    def _tool_configure_wiki(self, vault_path: str) -> Dict:
        """配置 Wiki/Obsidian 路径"""
        return self._facade.configure_wiki(vault_path)

    def _tool_detect_sources(self) -> Dict:
        """检测所有数据源状态"""
        return self._facade.detect_sources()

    # ---- JSON-RPC 2.0 / MCP 协议处理 ----

    def _make_jsonrpc_response(self, request_id: Any, result: Dict) -> Dict:
        """构建标准 JSON-RPC 2.0 成功响应"""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    def _make_jsonrpc_error(
        self, request_id: Any, code: int, message: str, data: Any | None = None
    ) -> Dict:
        """构建标准 JSON-RPC 2.0 错误响应"""
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        }

    def _make_tool_result(self, result: Any, *, is_error: bool = False) -> Dict:
        """构建 MCP CallToolResult。

        MCP 的 tools/call result 不能直接返回业务 dict；宿主客户端期望
        {"content": [{"type": "text", "text": "..."}]}。业务结构化数据以
        JSON 文本承载，兼容 2024-11-05 及更老客户端。
        """
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        payload = {
            "content": [{"type": "text", "text": text}],
        }
        if is_error:
            payload["isError"] = True  # type: ignore[assignment]
        return payload

    def handle_request(self, request: Dict) -> Optional[Dict]:
        """处理单个 JSON-RPC 请求/通知"""
        # 验证 JSON-RPC 版本
        if request.get("jsonrpc") != "2.0":
            return self._make_jsonrpc_error(
                request.get("id"), JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC version, expected 2.0"
            )

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        # JSON-RPC Notification: 没有 id，不需要返回响应
        is_notification = req_id is None

        if method == "initialize":
            return self._make_jsonrpc_response(req_id, self._handle_initialize(params))

        # 处理 notifications/initialized 通知（MCP 协议要求不返回响应）
        if method == "notifications/initialized":
            logger.debug("Received notifications/initialized")
            return None  # 通知不返回响应

        if method == "tools/list":
            return self._make_jsonrpc_response(req_id, self._list_tools())

        if method == "tools/call":
            if not isinstance(params, dict):
                return self._make_jsonrpc_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    "Invalid params: tools/call params must be an object",
                )
            tool_name = params.get("name", "")
            tool_params = params.get("arguments", {})
            if not isinstance(tool_params, dict):
                return self._make_jsonrpc_error(
                    req_id,
                    JSONRPC_INVALID_PARAMS,
                    "Invalid params: tools/call arguments must be an object",
                )
            return self._call_tool(req_id, tool_name, tool_params)

        # 对于未知方法，如果是通知也不返回错误
        if is_notification:
            logger.warning("Unknown notification: %s", method)
            return None

        return self._make_jsonrpc_error(
            req_id, JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {method}"
        )

    def _handle_initialize(self, params: Dict) -> Dict:
        """处理 initialize 握手（MCP 协议第一步）"""
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {
                "tools": {},
                "toolCategories": list(self._TOOL_CATEGORIES.keys()),
            },
            "serverInfo": {
                "name": "mnemos-mcp-server",
                "version": "2.0.0",
            },
        }

    def _list_tools(self) -> Dict:
        """列出所有可用 tools（带完整 inputSchema）"""
        return _schema_tools.list_tools(self._get_tool_category)

    def _call_tool(self, req_id: Any, name: str, params: Dict) -> Dict:
        """调用指定 tool，返回 JSON-RPC 包装响应"""
        if name not in self.tools:
            return self._make_jsonrpc_response(
                req_id,
                self._make_tool_result(f"Unknown tool: {name}", is_error=True),
            )

        if self._principal is None:
            return self._make_jsonrpc_response(
                req_id,
                self._make_tool_result(
                    {
                        "success": False,
                        "code": "principal_required",
                        "tool": name,
                    },
                    is_error=True,
                ),
            )

        if self._authorization_store is not None and self._launch_credential:
            try:
                current_principal = self._authorization_store.resolve_mcp_principal(
                    self._launch_credential
                )
            except (OSError, ValueError, sqlite3.Error):
                current_principal = None
            if current_principal is None:
                return self._make_jsonrpc_response(
                    req_id,
                    self._make_tool_result(
                        {
                            "success": False,
                            "code": "principal_revoked_or_expired",
                            "tool": name,
                        },
                        is_error=True,
                    ),
                )
            self._principal = current_principal

        authorization = authorize_tool_call(self._principal, name, params)
        if not authorization.allowed:
            return self._make_jsonrpc_response(
                req_id,
                self._make_tool_result(
                    {
                        "success": False,
                        "code": authorization.reason,
                        "tool": name,
                    },
                    is_error=True,
                ),
            )

        unknown_arguments = sorted(set(authorization.arguments) - self._tool_input_properties[name])
        if unknown_arguments:
            return self._make_jsonrpc_response(
                req_id,
                self._make_tool_result(
                    {
                        "success": False,
                        "code": "unknown_arguments",
                        "tool": name,
                        "arguments": unknown_arguments,
                    },
                    is_error=True,
                ),
            )

        try:
            result = self.tools[name](**authorization.arguments)
            return self._make_jsonrpc_response(req_id, self._make_tool_result(result))
        except TypeError as e:
            logger.warning("Tool parameter error: %s", e, exc_info=True)
            return self._make_jsonrpc_response(
                req_id,
                self._make_tool_result(
                    f"Invalid parameters for tool '{name}': {e}",
                    is_error=True,
                ),
            )
        except MCP_RECOVERABLE_ERRORS as e:
            logger.error("Tool execution error (%s): %s", name, e, exc_info=True)
            return self._make_jsonrpc_response(
                req_id,
                self._make_tool_result(
                    {
                        "code": MCP_TOOL_EXECUTION_ERROR,
                        "error": f"Tool '{name}' execution failed: {e}",
                        "tool": name,
                    },
                    is_error=True,
                ),
            )

    def run(self):
        """主循环 - 从 stdin 读取 JSON-RPC，写入 stdout"""
        logger.info("MCP server started (stdio mode, JSON-RPC 2.0)")

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                request = json.loads(line)
                response = self.handle_request(request)

                # JSON-RPC Notification 不返回响应
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False), flush=True)

            except json.JSONDecodeError as e:
                resp = self._make_jsonrpc_error(None, JSONRPC_PARSE_ERROR, f"Parse error: {e}")
                print(json.dumps(resp, ensure_ascii=False), flush=True)
            except MCP_RECOVERABLE_ERRORS as e:
                logger.error("Unexpected error: %s", e, exc_info=True)
                resp = self._make_jsonrpc_error(
                    None, JSONRPC_INTERNAL_ERROR, f"Internal error: {e}"
                )
                print(json.dumps(resp, ensure_ascii=False), flush=True)

        logger.info("MCP server stopped")


def build_mcp_server_from_environment(
    facade: MnemosServiceFacade | None = None,
    *,
    authorization_store: AgentAuthorizationStore | None = None,
    credential_store: MCPLaunchCredentialStore | None = None,
) -> MCPServer:
    """Build a stdio server from a keyring reference captured at startup."""
    from core.config import get_config

    # The host puts the non-secret reference in the child process environment.
    # Read that boundary directly: a long-lived embedded process may have
    # instantiated Config before a host refreshes its MCP environment, while a
    # new stdio server must still resolve the reference at its own cold start.
    # Keep the Config lookup as a compatibility fallback for embedded callers.
    reference = str(environment_get(MCP_LAUNCH_CAPABILITY_REF_ENV, "")).strip()
    if not reference:
        config = get_config()
        reference = str(
            config.get_runtime_environment(MCP_LAUNCH_CAPABILITY_REF_ENV)
        ).strip()
    launch_credential = ""
    if reference:
        try:
            launch_credential = (credential_store or MCPLaunchCredentialStore()).resolve(reference)
        except (ImportError, OSError, RuntimeError, ValueError):
            logger.warning("MCP launch credential reference unavailable", exc_info=True)
    return MCPServer(
        facade,
        launch_credential=launch_credential,
        authorization_store=authorization_store,
    )


def run_mcp_server():
    """Run the MCP writer while excluding offline migrations for its lifetime."""
    from core.config import get_config
    from core.ops.offline_migration_lock import runtime_writer_lock

    with runtime_writer_lock(get_config().database_dir):
        server = build_mcp_server_from_environment()
        server.run()


if __name__ == "__main__":
    run_mcp_server()
