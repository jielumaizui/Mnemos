# AgentDistillAdapter — 平台无关的宿主 Agent 蒸馏适配器
#
# 设计目标：通用版不假设用户用什么平台，自动检测并适配。
#
# 分层策略：
#   Tier 1: API 蒸馏（SiliconFlow/OpenAI/DeepSeek 等）— 全自动，体验最佳
#   Tier 2: 利用现有 Agent CLI（kimi --print / claude -p 等）— 免费，需平台适配
#   Tier 3: 占位符模式 — 保底，下次开 Agent 时提醒手动执行
#
# 平台适配矩阵：
#   Kimi    → kimi --print --final-message-only
#   Claude  → 占位符（无后台进程）
#   Cursor  → 占位符（无公开 CLI）
#   其他     → 占位符 + 手动 CLI 命令

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BaseDistillAdapter(ABC):
    """蒸馏适配器基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检测当前环境是否可用"""
        ...

    @abstractmethod
    def distill(self, session_id: str, messages: List[Dict], meta: Dict) -> Optional[str]:
        """执行蒸馏，返回蒸馏结果的 markdown/JSON 文本

        Returns:
            成功时返回蒸馏结果文本，失败时返回 None
        """
        ...

    def health_check(self) -> Dict[str, any]:
        """健康检查"""
        return {"name": self.name, "available": self.is_available()}


class APIDistillAdapter(BaseDistillAdapter):
    """Tier 1: API 蒸馏适配器

    复用 DistillationEngine + HostAgentCaller，通过配置的外部 API 执行蒸馏。
    """

    name = "api"

    def is_available(self) -> bool:
        from core.config import get_config
        cfg = get_config()
        # 检查是否有外部 API 配置
        providers = cfg.get("llm.providers", {})
        for key, val in providers.items():
            if val and val.get("api_key"):
                return True
        # 检查环境变量
        if os.getenv("SILICONFLOW_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"):
            return True
        return False

    def distill(self, session_id: str, messages: List[Dict], meta: Dict) -> Optional[str]:
        try:
            from core.hephaestus.distillation_engine import (
                DistillationEngine, HostAgentCaller
            )
            caller = HostAgentCaller(force_provider="api")
            engine = DistillationEngine(caller=caller)
            result = engine.process(session_id=session_id, messages=messages, meta=meta)
            # 将 result 序列化为 JSON 文本，供后续 write_pages 复用
            return result.to_json() if hasattr(result, "to_json") else json.dumps(
                {
                    "judgment": result.judgment,
                    "fragments": [f.__dict__ if hasattr(f, "__dict__") else f for f in (result.fragments or [])],
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.warning(f"[APIAdapter] 蒸馏失败: {e}")
            return None

    def health_check(self) -> Dict[str, any]:
        hc = super().health_check()
        try:
            from core.config import get_config
            cfg = get_config()
            providers = cfg.get("llm.providers", {})
            active = [k for k, v in providers.items() if v and v.get("api_key")]
            hc["active_providers"] = active
        except Exception:
            pass
        return hc


class KimiDistillAdapter(BaseDistillAdapter):
    """Tier 2: Kimi CLI 蒸馏适配器

    利用 `kimi --print --final-message-only` 非交互式调用 Kimi Coding Plan 额度。
    """

    name = "kimi"
    _cli_path: Optional[str] = None

    def _find_kimi(self) -> Optional[str]:
        if self._cli_path:
            return self._cli_path
        for candidate in ["kimi", str(Path.home() / ".local" / "bin" / "kimi")]:
            if shutil.which(candidate):
                self._cli_path = shutil.which(candidate)
                return self._cli_path
        return None

    def is_available(self) -> bool:
        return self._find_kimi() is not None

    def distill(self, session_id: str, messages: List[Dict], meta: Dict) -> Optional[str]:
        kimi = self._find_kimi()
        if not kimi:
            return None

        prompt = self._build_prompt(messages, meta)
        try:
            result = subprocess.run(
                [kimi, "--print", "--final-message-only"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                if "rate_limit" in result.stderr.lower():
                    logger.warning("[KimiAdapter] Rate limited")
                else:
                    logger.warning(f"[KimiAdapter] kimi failed: {result.stderr[:200]}")
                return None
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.warning("[KimiAdapter] Timeout")
            return None
        except Exception as e:
            logger.warning(f"[KimiAdapter] Exception: {e}")
            return None

    def _build_prompt(self, messages: List[Dict], meta: Dict) -> str:
        """构建 Kimi 蒸馏 prompt"""
        # 复用 DistillationEngine 的 prompt 逻辑
        try:
            from core.hephaestus.distillation_engine import DISTILLATION_PROMPT
        except ImportError:
            DISTILLATION_PROMPT = """你是一位知识蒸馏专家。请将以下对话提炼成结构化的知识。

要求：
1. 首先判断：这段对话是否包含值得长期保存的知识（knowledge）、技能（skill）还是无价值（skip）
2. 如果判定为 knowledge 或 skill，提取核心内容，输出为 JSON
3. JSON 格式：
{
  "judgment": "knowledge|skill|skip",
  "fragments": [
    {
      "form": "概念/经验/决策/配置/报错/最佳实践",
      "title": "标题",
      "frontmatter": {"tags": []},
      "background": "背景",
      "core_content": "核心内容",
      "boundaries": {"适用": "", "不适用": ""},
      "anti_patterns": [],
      "related_concepts": []
    }
  ]
}
4. 不要编造内容，严格基于对话
"""
        # 序列化消息
        msg_text = "\n\n".join(
            f"[{m.get('role', 'unknown')}] {m.get('content', '')[:500]}"
            for m in messages
        )
        source = meta.get("source", "unknown")
        return f"{DISTILLATION_PROMPT}\n\n来源: {source}\n\n对话内容:\n\n{msg_text}\n\n请输出 JSON:"

    def health_check(self) -> Dict[str, any]:
        hc = super().health_check()
        hc["cli_path"] = self._find_kimi()
        return hc


class ClaudeDistillAdapter(BaseDistillAdapter):
    """Tier 2: Claude Code 占位符适配器

    Claude Code 无后台进程，只能写占位符到任务目录，等用户下次打开 Claude 时执行。
    """

    name = "claude"

    def is_available(self) -> bool:
        return shutil.which("claude") is not None or (Path.home() / ".claude").exists()

    def distill(self, session_id: str, messages: List[Dict], meta: Dict) -> Optional[str]:
        # Claude 无法后台执行，返回特殊标记
        return self._write_placeholder(session_id, messages, meta)

    def _write_placeholder(self, session_id: str, messages: List[Dict], meta: Dict) -> str:
        """写占位符到 Claude 的任务目录"""
        task_dir = Path.home() / ".claude" / "mnemos_distill_tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        task_path = task_dir / f"{session_id}.md"

        source = meta.get("source", "unknown")
        msg_count = len(messages)

        placeholder = f"""<!-- MNEMOS_DISTILL_TASK: {session_id} -->
# Mnemos 蒸馏任务（等待执行）

**Session**: `{session_id}`
**来源**: {source}
**消息数**: {msg_count}
**创建时间**: {__import__('datetime').datetime.now().isoformat()}

## 执行指令

你有一个 Mnemos 蒸馏任务待处理。请运行：

```bash
python3 -m mnemos_cli distill --task {session_id}
```

或自动模式：
```bash
python3 -m mnemos_cli distill --auto
```

---

*此文件由 Mnemos 自动生成，请勿手动编辑。*
"""
        task_path.write_text(placeholder, encoding="utf-8")
        logger.info(f"[ClaudeAdapter] 占位符已写入: {task_path}")
        return placeholder

    def health_check(self) -> Dict[str, any]:
        hc = super().health_check()
        hc["has_cli"] = shutil.which("claude") is not None
        hc["has_data_dir"] = (Path.home() / ".claude").exists()
        return hc


class GenericDistillAdapter(BaseDistillAdapter):
    """Tier 3: 通用占位符适配器

    当没有任何可用的 Agent 或 API 时，写入提醒文件，引导用户手动触发。
    """

    name = "generic"

    def is_available(self) -> bool:
        return True  # 总是可用，作为保底

    def distill(self, session_id: str, messages: List[Dict], meta: Dict) -> Optional[str]:
        reminder_dir = Path.home() / ".mnemos" / "distill_reminders"
        reminder_dir.mkdir(parents=True, exist_ok=True)
        reminder_path = reminder_dir / f"{session_id}.md"

        source = meta.get("source", "unknown")
        placeholder = f"""<!-- MNEMOS_DISTILL_TASK: {session_id} -->
# Mnemos 蒸馏任务（需手动触发）

**Session**: `{session_id}`
**来源**: {source}
**消息数**: {len(messages)}

## 如何执行

你的系统未配置自动蒸馏。请选择一个方式：

### 方式 A：配置 API（推荐）
```bash
# 配置 SiliconFlow / OpenAI / DeepSeek API
mnemos config set llm.providers.siliconflow.api_key YOUR_KEY
```

### 方式 B：利用现有 Agent
如果你使用 Kimi Code CLI：
```bash
mnemos setup --distill-via=kimi
```

### 方式 C：手动蒸馏
```bash
mnemos distill --session {session_id}
```

---

*此文件由 Mnemos 自动生成。*
"""
        reminder_path.write_text(placeholder, encoding="utf-8")
        logger.info(f"[GenericAdapter] 提醒已写入: {reminder_path}")
        return placeholder


class AgentDistillAdapter:
    """平台无关的蒸馏适配器 — 自动检测环境，选择最佳方案

    Usage:
        adapter = AgentDistillAdapter()
        result = adapter.auto_distill(session_id, messages, meta)
        if result:
            # 解析结果，写入 wiki
            ...
    """

    def __init__(self):
        self.adapters: List[BaseDistillAdapter] = [
            APIDistillAdapter(),
            KimiDistillAdapter(),
            ClaudeDistillAdapter(),
            GenericDistillAdapter(),
        ]
        self._preferred: Optional[str] = None

    def set_preferred(self, name: str):
        """手动设置优先适配器（用户配置）"""
        self._preferred = name.lower()

    def detect_environment(self) -> Dict[str, any]:
        """检测当前环境的蒸馏能力"""
        env = {
            "adapters": [],
            "recommended": None,
            "has_api": False,
            "has_agent_cli": False,
        }
        for adapter in self.adapters:
            hc = adapter.health_check()
            env["adapters"].append(hc)
            if hc["available"]:
                if adapter.name == "api":
                    env["has_api"] = True
                elif adapter.name in ("kimi", "claude"):
                    env["has_agent_cli"] = True
                if env["recommended"] is None:
                    env["recommended"] = adapter.name
        return env

    def auto_distill(self, session_id: str, messages: List[Dict], meta: Dict) -> Tuple[Optional[str], str]:
        """自动选择最佳适配器执行蒸馏

        Returns:
            (result_text, adapter_name)
            result_text: 蒸馏结果，None 表示失败
            adapter_name: 实际使用的适配器名称
        """
        # 1. 用户手动指定
        if self._preferred:
            for adapter in self.adapters:
                if adapter.name == self._preferred and adapter.is_available():
                    logger.info(f"[AgentDistillAdapter] 使用用户指定适配器: {adapter.name}")
                    result = adapter.distill(session_id, messages, meta)
                    return result, adapter.name
            logger.warning(f"[AgentDistillAdapter] 用户指定的适配器 {self._preferred} 不可用")

        # 2. 按优先级自动选择
        for adapter in self.adapters:
            if adapter.is_available():
                logger.info(f"[AgentDistillAdapter] 使用适配器: {adapter.name}")
                result = adapter.distill(session_id, messages, meta)
                if result:
                    return result, adapter.name
                logger.warning(f"[AgentDistillAdapter] {adapter.name} 蒸馏失败，尝试下一个")

        return None, "none"

    def is_fully_automatic(self) -> bool:
        """检查当前环境是否支持全自动蒸馏（无需用户手动干预）"""
        for adapter in self.adapters:
            if adapter.name in ("api", "kimi") and adapter.is_available():
                return True
        return False


# ── 便捷函数 ──

def get_adapter() -> AgentDistillAdapter:
    """获取全局 AgentDistillAdapter 实例"""
    return AgentDistillAdapter()


def quick_distill(session_id: str, messages: List[Dict], meta: Dict) -> Tuple[Optional[str], str]:
    """一键蒸馏，自动选择最佳方案"""
    return get_adapter().auto_distill(session_id, messages, meta)
