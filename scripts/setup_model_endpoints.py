"""Model endpoint setup helpers shared by the auto setup script."""

from __future__ import annotations

import getpass
import os
from typing import Callable, Dict


OPTIONAL_MODEL_ENDPOINT_SPECS = {
    "multimodal": {
        "label": "多模态模型（图片/截图/视觉证据解析）",
        "config_section": "multimodal",
        "env_prefix": "MNEMOS_MULTIMODAL",
    },
}


def setup_optional_multimodal(
    data: dict,
    yes_mode: bool,
    *,
    ask: Callable[[str, str], str],
    ask_yes_no: Callable[[str], bool],
    configure_model_endpoint: Callable[..., None],
    store_model_key_in_keyring: Callable[[str, str], str],
    print_ok: Callable[[str], None],
    print_warn: Callable[[str], None],
) -> None:
    """Configure the optional multimodal endpoint without blocking setup."""
    section = data.setdefault("multimodal", {})
    section.setdefault("enabled", False)
    section.setdefault("provider", "siliconflow")
    section.setdefault("base_url", "https://api.siliconflow.cn/v1")
    section.setdefault("api_key", "")
    section.setdefault("api_key_env", "MNEMOS_MULTIMODAL_API_KEY")
    section.setdefault("api_key_source", "")
    section.setdefault("model", "Qwen/Qwen2.5-VL-72B-Instruct")
    section.setdefault("timeout", 120)
    section.setdefault("chain", [])

    if (
        os.environ.get("MNEMOS_MULTIMODAL_API_KEY")
        and os.environ.get("MNEMOS_MULTIMODAL_BASE_URL")
        and os.environ.get("MNEMOS_MULTIMODAL_MODEL")
    ):
        configure_model_endpoint(
            data,
            "multimodal",
            base_url=os.environ["MNEMOS_MULTIMODAL_BASE_URL"],
            model=os.environ["MNEMOS_MULTIMODAL_MODEL"],
            api_key_source="env:MNEMOS_MULTIMODAL_API_KEY",
            api_key_env="MNEMOS_MULTIMODAL_API_KEY",
        )
        print_ok("多模态模型已自动启用（从 MNEMOS_MULTIMODAL_API_KEY 读取）")
        return

    if yes_mode:
        section["enabled"] = False
        print_warn("跳过可选多模态模型；不影响 Mnemos 正常使用")
        return

    print("\n  [可选] 多模态模型，可跳过，不影响 Mnemos 正常使用")
    if not ask_yes_no("是否现在配置图片/截图/视觉证据解析模型？"):
        section["enabled"] = False
        print_warn("跳过可选多模态模型；之后可设置 MNEMOS_MULTIMODAL_* 环境变量再启用")
        return

    spec = OPTIONAL_MODEL_ENDPOINT_SPECS["multimodal"]
    base_url = ask(
        f"{spec['label']} API 地址(base_url):",
        str(section.get("base_url", "") or ""),
    ).strip()
    model = ask(
        f"{spec['label']} 模型 ID:",
        str(section.get("model", "") or ""),
    ).strip()
    api_key = getpass.getpass(f"  {spec['label']} API Key: ").strip()
    if not (base_url and model and api_key):
        section["enabled"] = False
        print_warn("多模态模型未配置完整，已跳过；不影响 Mnemos 正常使用")
        return

    configure_model_endpoint(
        data,
        "multimodal",
        base_url=base_url,
        model=model,
        api_key_source=store_model_key_in_keyring("multimodal", api_key),
    )
    print_ok("可选多模态模型已配置")


def detect_api_configs() -> Dict[str, bool]:
    """Detect configured API keys from env and runtime config."""
    apis = {
        "llm": bool(
            os.getenv("MNEMOS_LLM_API_KEY")
            or os.getenv("SILICONFLOW_API_KEY")
            or os.getenv("DMXAPI_API_KEY")
            or os.getenv("DMX_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        ),
        "embedding": bool(os.getenv("MNEMOS_EMBEDDING_API_KEY")),
        "reranker": bool(os.getenv("MNEMOS_RERANKER_API_KEY")),
        "multimodal": bool(os.getenv("MNEMOS_MULTIMODAL_API_KEY")),
        "siliconflow": bool(os.getenv("SILICONFLOW_API_KEY")),
        "dmxapi": bool(os.getenv("DMXAPI_API_KEY") or os.getenv("DMX_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "deepseek": bool(os.getenv("DEEPSEEK_API_KEY")),
    }
    try:
        from core.config import get_config
        from core import llm_config

        cfg = get_config()
        chain = llm_config.resolve_llm_api_chain(cfg)
        if any(item.configured for item in chain.all_configs):
            apis["llm"] = True
            apis[chain.primary.provider] = True
        embedding_cfg = llm_config.resolve_embedding_api_config(cfg)
        reranker_cfg = llm_config.resolve_reranker_api_config(cfg)
        multimodal_cfg = llm_config.resolve_multimodal_api_config(cfg)
        apis["embedding"] = apis["embedding"] or embedding_cfg.configured
        apis["reranker"] = apis["reranker"] or reranker_cfg.configured
        apis["multimodal"] = apis["multimodal"] or multimodal_cfg.configured
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
    ):
        pass
    return apis
