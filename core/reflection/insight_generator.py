"""
Insight Generator — 洞察生成器

核心职责：
基于 Mirror 证据链，调用 LLM 生成可解释的洞察。

设计原则：
- 运行时生成，用完即走（不存储 Insight 全文）
- 只存储 Insight 摘要（在 ReflectionRecord 中）
- 提示词必须包含时间上下文（让 LLM 知道数据的时效性）
- 提示词必须要求 Insight 基于证据（禁止凭空编造）

实现方式：
- 目前使用 prompt-based 方式（不直接调用 LLM API，由宿主 Agent 执行）
- 未来可接入 Hephaestus Worker 的 LLM API 调用能力
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.llm_config import resolve_llm_api_chain
from core.config import get_config
from core.reflection.models import InsightSnapshot
from core.reflection.mirror_engine import MirrorResult
from core.reflection.time_awareness import TemporalContext
from core.reflection.experience_matcher import ExperienceMatch
from core.telemetry.prompt_call_log import (
    ModelCallLedger,
    ModelCallReservation,
    metered_provider_usage,
)
from core.telemetry.provider_request import (
    canonical_chat_input,
    non_redirecting_openai_client,
    safe_provider_error_category,
    utf8_token_upper_bound,
)

logger = logging.getLogger(__name__)


@dataclass
class InsightResult:
    """洞察生成结果"""

    summary: str = ""  # 一句话摘要
    key_points: List[str] = field(default_factory=list)  # 关键结论
    dimensions_involved: List[str] = field(default_factory=list)
    confidence: float = 0.0  # 洞察置信度（基于证据质量）
    prompt_used: str = ""  # 使用的提示词（用于调试）
    generated_at: datetime = field(default_factory=datetime.now)
    calibration_note: str = ""  # 校准备注（如低置信度提示）
    llm_called: bool = False  # 是否实际调用了 LLM
    llm_error: str = ""  # LLM 调用失败原因（空字符串表示无错误）

    def to_snapshot(self) -> InsightSnapshot:
        """转换为可存储的快照"""
        return InsightSnapshot(
            summary=self.summary,
            key_points=self.key_points,
            dimensions_involved=self.dimensions_involved,
        )


class InsightGenerator:
    """洞察生成引擎"""

    def __init__(self, use_llm: bool = True):
        self.use_llm = use_llm

    def generate(
        self,
        mirror: MirrorResult,
        temporal: Optional[TemporalContext] = None,
        user_query: str = "",
        calibration_hints: str = "",
        min_confidence: float = 0.0,
        experiences: Optional[List[ExperienceMatch]] = None,
        subject_scope: tuple[str, str] | None = None,
    ) -> InsightResult:
        """
        生成洞察

        注意：这个方法构建提示词，但不直接调用 LLM。
        宿主 Agent 拿到提示词后执行推理。

        Args:
            mirror: Mirror 证据链
            temporal: 时间上下文
            user_query: 用户的原始输入
            experiences: 相似历史经验（可选）

        Returns:
            InsightResult（包含 prompt_used，由宿主 Agent 填充 summary/key_points）
        """
        # 1. 计算洞察置信度
        confidence = self._calculate_confidence(mirror)

        # 2. 应用校准阈值（如果设置了最低置信度且不达标，标记为低置信度）
        low_confidence_flag = False
        if min_confidence > 0 and confidence < min_confidence:
            low_confidence_flag = True
            confidence = round(confidence, 2)

        # 3. 构建提示词（注入校准指令 + 历史相似事件）
        prompt = self._build_prompt(mirror, temporal, user_query, calibration_hints, experiences)

        # 4. 尝试调用 LLM 填充洞察内容
        llm_called = self.use_llm
        llm_error = ""
        if self.use_llm:
            raw_response = (
                self._call_llm(prompt, subject_scope=subject_scope)
                if subject_scope is not None
                else self._call_llm(prompt)
            )
        else:
            raw_response = None
        if self.use_llm and raw_response is None:
            llm_error = "LLM 调用未返回有效内容（可能未配置 API、openai SDK 未安装或调用失败）"
        parsed = self._parse_response(raw_response) if raw_response else {}

        # 5. 返回结果
        result = InsightResult(
            summary=parsed.get("summary", ""),
            key_points=parsed.get("key_points", []),
            dimensions_involved=mirror.dimensions_involved,
            confidence=confidence,
            prompt_used=prompt,
            llm_called=llm_called,
            llm_error=llm_error,
        )
        if low_confidence_flag:
            result.calibration_note = (
                f"洞察置信度 ({confidence}) 低于校准阈值 ({min_confidence})，"
                "建议谨慎参考或补充更多证据"
            )
        return result

    def _calculate_confidence(self, mirror: MirrorResult) -> float:
        """
        计算洞察置信度

        基于：
        - 证据链的数量
        - 证据的时效权重
        - 证据的置信度
        """
        if not mirror.snapshots:
            return 0.0

        # 基础分：证据数量
        count_score = min(1.0, len(mirror.snapshots) / 5.0)

        # 时效分：平均时间衰减权重
        recency_score = sum(s.recency_weight for s in mirror.snapshots) / len(mirror.snapshots)

        # 置信分：平均证据置信度
        conf_score = sum(s.confidence for s in mirror.snapshots) / len(mirror.snapshots)

        # 综合分
        overall = count_score * 0.3 + recency_score * 0.4 + conf_score * 0.3
        return round(overall, 2)

    def _call_llm(
        self,
        prompt: str,
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> Optional[str]:
        """调用 LLM API 获取洞察文本，失败时返回 None。"""
        try:
            import openai
        except ImportError:
            logger.debug("openai SDK 未安装，InsightGenerator 无法调用 LLM")
            return None
        openai_error_type = getattr(openai, "OpenAIError", RuntimeError)

        try:
            runtime_config = get_config()
            chain = resolve_llm_api_chain(runtime_config)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("LLM 配置解析失败", exc_info=True)
            return None

        resolved_subject_scope = subject_scope or ("source", "reflection_insight")
        retry_attempt = 0
        for api_cfg in chain.all_configs:
            if not api_cfg.configured:
                continue
            active_cfg = api_cfg.active()
            if not active_cfg.configured:
                continue
            current_attempt = retry_attempt
            reservation: ModelCallReservation | None = None
            try:
                client_kwargs = {"api_key": active_cfg.api_key}
                if active_cfg.base_url:
                    client_kwargs["base_url"] = active_cfg.base_url
                messages = [{"role": "user", "content": prompt}]
                provider_input = canonical_chat_input(messages)
                ledger = ModelCallLedger.for_config(runtime_config)
                run_id = ledger.start_run(
                    f"reflection-insight:{uuid.uuid4().hex}",
                    subject_scope=resolved_subject_scope,
                )
                reservation = ledger.reserve(
                    run_id=run_id,
                    operation="reflection_insight",
                    provider=active_cfg.provider,
                    model=active_cfg.model,
                    input_text=provider_input,
                    input_tokens=utf8_token_upper_bound(provider_input),
                    output_tokens=2000,
                    cache_status="miss",
                    retry_attempt=current_attempt,
                    subject_scopes=(resolved_subject_scope,),
                )
                reservation.mark_dispatched()
                retry_attempt += 1
                started = time.perf_counter()
                with non_redirecting_openai_client(
                    openai.OpenAI, **client_kwargs
                ) as client:  # type: ignore[arg-type]
                    response = client.chat.completions.create(
                        model=active_cfg.model,
                        messages=messages,
                        max_tokens=2000,
                        temperature=0.3,
                        timeout=60,
                    )
                usage = getattr(response, "usage", None)
                request_id = str(getattr(response, "id", "") or "")
                if reservation is not None:
                    metered_usage = metered_provider_usage(
                        usage,
                        request_id=request_id,
                        output_required=True,
                    )
                    if metered_usage is None:
                        reservation.preserve_incurred(
                            error_code="reflection_provider_usage_missing"
                        )
                    else:
                        reservation.settle(
                            usage=metered_usage,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                        )
                content = response.choices[0].message.content
                if isinstance(content, str) and content:
                    api_cfg.report_success(active_cfg)
                    return content.strip()
            except (
                openai_error_type,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                IndexError,
                RuntimeError,
            ) as exc:
                if reservation is not None:
                    if reservation.dispatched:
                        reservation.preserve_incurred(error_code="reflection_provider_exception")
                    else:
                        reservation.release(error_code="reflection_pre_dispatch_exception")
                error_category = safe_provider_error_category(exc)
                api_cfg.report_failure(active_cfg, error_category)
                logger.warning(
                    "Insight LLM call failed: %s/%s category=%s",
                    active_cfg.provider,
                    active_cfg.model,
                    error_category,
                )
        return None

    def _parse_response(self, raw: str) -> Dict[str, Any]:
        """从 LLM 响应中解析一句话摘要和关键发现"""
        summary = ""
        key_points: List[str] = []
        in_summary = False
        in_key_points = False

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("### 一句话摘要"):
                in_summary = True
                in_key_points = False
                continue
            if stripped.startswith("### 关键发现"):
                in_key_points = True
                in_summary = False
                continue
            if stripped.startswith("###"):
                in_summary = False
                in_key_points = False
                continue

            if in_summary and not summary:
                summary = stripped.strip("* '").strip()
                continue

            if in_key_points:
                cleaned = stripped
                # 去掉前导编号 / 项目符号
                cleaned = re.sub(r"^[-\d\.\s、．]+", "", cleaned)
                # 去掉加粗 markdown
                cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned).strip()
                if cleaned:
                    key_points.append(cleaned)

        return {"summary": summary, "key_points": key_points}

    def _build_prompt(
        self,
        mirror: MirrorResult,
        temporal: Optional[TemporalContext],
        user_query: str,
        calibration_hints: str = "",
        experiences: Optional[List[ExperienceMatch]] = None,
    ) -> str:
        """构建 LLM 提示词"""
        lines = [
            "# Mnemos Reflection — 洞察生成",
            "",
            "## 角色",
            "你是一位认知分析师，擅长从长期行为数据中发现用户没有意识到的模式。",
            "你的分析必须基于下面的证据链，禁止凭空编造。",
            "",
        ]

        # 用户查询
        if user_query:
            lines.extend(
                [
                    "## 用户当前情境",
                    f"{user_query}",
                    "",
                ]
            )

        # 历史相似事件
        if experiences:
            lines.extend(
                [
                    "## 历史相似事件",
                    "以下是从用户历史 Reflection / 认知变迁 / 复盘中召回的相似事件，",
                    "请在分析中参考这些事件，帮助用户看到自己是否在重复某个模式。",
                    "",
                ]
            )
            for i, exp in enumerate(experiences, 1):
                lines.append(f"### 相似事件 {i}: {exp.title}（相似度 {exp.score:.2f}）")
                lines.append(f"- 来源: {exp.source_type} | ID: {exp.source_id}")
                lines.append(f"- 摘要: {exp.summary[:200]}")
                lines.append("")
            lines.append("")

        # 时间上下文
        if temporal:
            lines.extend(
                [
                    "## 时间上下文",
                    f"- 当前时间: {temporal.now_str}",
                    f"- 时间节律: {temporal.rhythm_description}",
                ]
            )
            if temporal.last_reflection_ago is not None:
                from core.reflection.time_awareness import TimeAwareness

                duration_text = TimeAwareness().humanize_duration(temporal.last_reflection_ago)
                lines.append(f"- 距离上次分析: {duration_text}")
            lines.append("")

        # 证据链
        lines.append(mirror.to_prompt_context())
        lines.append("")

        # 校准提示（基于用户反馈动态调整）
        if calibration_hints:
            lines.extend(
                [
                    "## 校准提示（基于历史反馈）",
                    calibration_hints,
                    "",
                ]
            )

        # 输出要求
        lines.extend(
            [
                "## 分析要求",
                "1. **必须基于证据**：每条结论都要追溯到上面的具体证据或历史相似事件",
                "2. **时间敏感**：注意证据的时效性（时效权重低的证据要谨慎引用）",
                "3. **模式优先**：寻找反复出现的模式，而不是单次事件",
                '4. **认知视角**：从"用户如何看待自己"的角度分析',
                "5. **适度推测**：可以基于证据做合理推测，但要标注不确定性",
                "",
                "## 输出格式",
                "### 一句话摘要",
                "（用一句话概括核心发现）",
                "",
                "### 关键发现",
                "1. **发现1**: 基于证据X，...",
                "2. **发现2**: 基于证据Y，...",
                "3. **发现3**（可选）: ...",
                "",
                "### 不确定性标注",
                "- （标注哪些结论证据不足，哪些是推测）",
            ]
        )

        return "\n".join(lines)
