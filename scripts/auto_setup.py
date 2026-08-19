#!/usr/bin/env python3
"""Mnemos 自动部署脚本 — 一键开箱即用。

用法:
    python3 scripts/auto_setup.py [--yes] [--skip-backend]

选项:
    --yes          全自动模式，不提示确认（适合 CI）
    --skip-backend   跳过 Raw Vault 后端确认步骤
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()

SETUP_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)


def _mnemos_dir() -> Path:
    """返回 Mnemos 数据目录，优先读取 MNEMOS_DIR 环境变量。"""
    return Path(os.environ.get("MNEMOS_DIR", str(Path.home() / ".mnemos"))).expanduser()


# 确保项目根目录在 sys.path 中，支持从任意位置运行
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.setup.vault_layout import default_mnemos_vault_path, default_raw_vault_path  # noqa: E402
from core.ops.durable_io import (  # noqa: E402
    DurableIOError,
    inspect_path_kind,
    secure_atomic_write_bytes,
)
from core.ops.durable_io import read_native_bytes  # noqa: E402
from core.utils import secure_directory, secure_file  # noqa: E402
from scripts.setup_model_endpoints import (  # noqa: E402
    OPTIONAL_MODEL_ENDPOINT_SPECS as _OPTIONAL_MODEL_ENDPOINT_SPECS,
    detect_api_configs as _detect_api_configs,
    setup_optional_multimodal,
)
from scripts.required_model_prompt import (  # noqa: E402
    DEFAULT_MAX_SMOKE_ATTEMPTS,
    RequiredModelEndpointSetupAbort as _RequiredModelEndpointSetupAbort,
    prompt_required_model_endpoints as _prompt_required_model_endpoints_impl,
)
from scripts.setup_dependencies import (  # noqa: E402
    ensure_venv as _ensure_venv_impl,
    install_dependencies as _install_dependencies_impl,
)
from scripts import auto_setup_config_support as _config_support  # noqa: E402

# 当前使用的 Python 解释器（可能被虚拟环境替换）
_PYTHON_EXE = sys.executable


class SetupAbort(RuntimeError):
    """Raised when setup cannot continue without required user configuration."""


class RequiredModelEndpointSetupAbort(_RequiredModelEndpointSetupAbort, SetupAbort):
    """SetupAbort-compatible required model endpoint failure."""


DOCUMENTS_HOME = "~/" + "Documents"
# 跨平台 Obsidian Vault 常见路径，仅用于首次设置时的发现候选。
OBSIDIAN_VAULT_PATHS = {
    "Darwin": [
        f"{DOCUMENTS_HOME}/Obsidian Vault",
        f"{DOCUMENTS_HOME}/Obsidian",
        "~/Library/Mobile Documents/iCloud~md~obsidian/Documents",
    ],
    "Linux": [
        f"{DOCUMENTS_HOME}/Obsidian Vault",
        f"{DOCUMENTS_HOME}/Obsidian",
        "~/obsidian",
    ],
    "Windows": [
        r"~\Documents" + r"\Obsidian Vault",
        r"~\Documents" + r"\Obsidian",
        r"~\Obsidian",
    ],
}


def print_step(n: int, total: int, title: str) -> None:
    print(f"\n{'='*60}")
    print(f"[{n}/{total}] {title}")
    print("=" * 60)


def print_ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def print_warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def print_err(msg: str) -> None:
    print(f"  ✗ {msg}")


def ask(prompt: str, default: str = "", yes_mode: bool = False) -> str:
    if yes_mode:
        print(f"  {prompt} (auto: {default or 'skip'})")
        return default
    try:
        return input(f"  {prompt} ").strip() or default
    except (EOFError, KeyboardInterrupt):
        return default


def ask_yes_no(prompt: str, default: bool = True, yes_mode: bool = False) -> bool:
    if yes_mode:
        print(f"  {prompt} (auto: {'yes' if default else 'no'})")
        return default
    default_str = "Y/n" if default else "y/N"
    ans = input(f"  {prompt} [{default_str}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def collect_dry_run_status() -> Dict[str, object]:
    """收集 dry-run 状态，不产生人读输出。"""
    installed, version, app_path = detect_obsidian_app()
    vaults = find_obsidian_vaults()
    python_ok = sys.version_info >= (3, 10)
    return {
        "ok": python_ok,
        "project_root": str(PROJECT_ROOT),
        "platform": {"system": platform.system(), "release": platform.release()},
        "python": {
            "version": sys.version.split()[0],
            "ok": python_ok,
        },
        "obsidian": {
            "installed": installed,
            "version": version,
            "path": str(app_path) if app_path else None,
            "download_url": None if installed else "https://obsidian.md/download",
        },
        "vaults": {
            "detected_count": len(vaults),
            "paths": [str(v) for v in vaults],
            "default_mnemos": str(DEFAULT_MNEMOS_VAULT),
            "default_raw": str(DEFAULT_RAW_VAULT),
        },
        "actions": [],
    }


# ── 步骤 1: Python 版本 ──


def check_python() -> bool:
    v = sys.version_info
    print(f"  Python {v.major}.{v.minor}.{v.micro}")
    if v >= (3, 10):
        print_ok("版本满足 >= 3.10")
        return True
    print_err(f"需要 Python >= 3.10，当前 {v.major}.{v.minor}")
    return False


# ── 步骤 2: 安装依赖 ──


def _ensure_venv() -> Optional[Path]:
    """在项目根目录创建 .venv，返回 venv 中的 python 路径。"""
    return _ensure_venv_impl(
        project_root=PROJECT_ROOT,
        python_executable=sys.executable,
        print_err=print_err,
        print_warn=print_warn,
    )


def install_dependencies(yes_mode=False, reexec_args=None, reexec_entrypoint=None) -> bool:
    global _PYTHON_EXE
    outcome = _install_dependencies_impl(
        project_root=PROJECT_ROOT,
        python_exe=_PYTHON_EXE,
        reexec_args=reexec_args,
        reexec_entrypoint=reexec_entrypoint or str(Path(__file__).resolve()),
        ensure_venv_func=_ensure_venv,
        print_ok=print_ok,
        print_warn=print_warn,
        print_err=print_err,
    )
    _PYTHON_EXE = outcome.python_exe
    if outcome.should_reexec and outcome.reexec_script is not None:
        os.execv(
            _PYTHON_EXE,
            [_PYTHON_EXE, str(outcome.reexec_script), *outcome.reexec_argv],
        )
    return outcome.ok


# ── 步骤 3: 检测 Raw Vault 后端 ──


def setup_l1_backend(skip: bool = False, yes_mode: bool = False) -> bool:
    if skip:
        print_warn("跳过 Raw Vault 后端检测")
        return True

    print("  检测 Raw Vault 后端...")
    print_ok("Raw Vault 使用 ObsidianBackend（本地 Markdown 文件），无需外部服务")
    return True


# ── 步骤 4: 检测 Obsidian ──

OBSIDIAN_INSTALL_PATHS = {
    "Darwin": [
        "/Applications/Obsidian.app",
        "~/Applications/Obsidian.app",
    ],
    "Windows": [
        r"~\AppData\Local\Obsidian\Obsidian.exe",
    ],
    "Linux": [
        "/usr/bin/obsidian",
        "/usr/local/bin/obsidian",
        "~/.local/share/obsidian/obsidian",
    ],
}


def _read_macos_app_version(app_path: Path) -> Optional[str]:
    """从 macOS .app 的 Info.plist 读取版本号"""
    plist = app_path / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    try:
        result = subprocess.run(
            ["plutil", "-extract", "CFBundleShortVersionString", "raw", str(plist)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def detect_obsidian_app() -> Tuple[bool, Optional[str], Optional[Path]]:
    """检测系统是否安装了 Obsidian 应用。

    Returns:
        (installed, version_or_none, path_or_none)
    """
    system = platform.system()
    candidates = OBSIDIAN_INSTALL_PATHS.get(system, [])
    for p in candidates:
        path = Path(p).expanduser()
        if path.exists():
            version = None
            if system == "Darwin":
                version = _read_macos_app_version(path)
            return True, version, path

    # Linux fallback: which
    obsidian_path = shutil.which("obsidian")
    if system == "Linux" and obsidian_path:
        return True, None, Path(obsidian_path)

    return False, None, None


def find_obsidian_vaults() -> List[Path]:
    """扫描常见路径查找 Obsidian Vault"""
    found = []
    system = platform.system()
    paths = OBSIDIAN_VAULT_PATHS.get(system, [])
    for p in paths:
        expanded = Path(p).expanduser()
        if expanded.exists() and expanded.is_dir():
            # 检查是否真的是 vault（有 .obsidian 目录或 Markdown 文件）
            if (expanded / ".obsidian").exists() or list(expanded.glob("*.md")):
                found.append(expanded)
    return found


DEFAULT_MNEMOS_VAULT = default_mnemos_vault_path()
DEFAULT_RAW_VAULT = default_raw_vault_path()


def setup_obsidian(yes_mode: bool = False) -> Tuple[bool, Optional[Path], Optional[Path]]:
    """检测 Obsidian 应用并确认两个 Vault 路径（mnemos 主认知 Vault + raw L1 Vault）。"""
    installed, version, app_path = detect_obsidian_app()
    if installed:
        ver_str = f" v{version}" if version else ""
        print_ok(f"Obsidian 已安装{ver_str} ({app_path})")
    else:
        print_err("未检测到 Obsidian 应用")
        print("  Obsidian 是 Mnemos 部署阶段的必装依赖，用于打开 raw 与 Mnemos 两个本地 Vault。")
        print("  Mnemos 现在会停止部署，避免继续写入一套用户无法查看、接管和维护的半成品知识库配置。")
        print("  原因：raw Vault 保存各 Agent 的原始对话，Mnemos Vault 保存蒸馏后的认知库；两者都需要能被 Obsidian 打开并人工核验。")
        print("  下一步：请先安装 Obsidian，然后重新运行 setup。")
        print("  下载地址： https://obsidian.md/download")
        print("部署已停止：缺少 Obsidian 会导致 Mnemos 的本地知识库体验不完整。")
        return False, None, None

    print("  配置 Mnemos Vault 路径...")
    if yes_mode:
        mnemos_vault = DEFAULT_MNEMOS_VAULT
        raw_vault = DEFAULT_RAW_VAULT
        print(f"  使用默认 Mnemos Vault: {mnemos_vault}")
        print(f"  使用默认 raw Vault: {raw_vault}")
    else:
        mnemos_vault = Path(
            ask(
                f"Mnemos 主认知 Vault 路径 (回车={DEFAULT_MNEMOS_VAULT}):",
                default=str(DEFAULT_MNEMOS_VAULT),
                yes_mode=yes_mode,
            )
        )
        raw_vault = Path(
            ask(
                f"raw 原始记录 Vault 路径 (回车={DEFAULT_RAW_VAULT}):",
                default=str(DEFAULT_RAW_VAULT),
                yes_mode=yes_mode,
            )
        )

    # 统一转为绝对路径
    mnemos_vault = mnemos_vault.expanduser().resolve()
    raw_vault = raw_vault.expanduser().resolve()
    print_ok(f"Mnemos Vault: {mnemos_vault}")
    print_ok(f"raw Vault: {raw_vault}")
    return True, mnemos_vault, raw_vault


# ── 步骤 5: 生成配置 ──


def _runtime_config_path() -> Path:
    """运行时权威配置路径，与 core.config.Config 保持一致。"""
    return _mnemos_dir() / "configs" / "main.json"


def _deep_merge(
    base: dict,
    override: dict,
    _visited: Optional[Set[int]] = None,
) -> None:
    _config_support.deep_merge(base, override, _visited)


_VALID_PERFORMANCE_TIERS = _config_support.VALID_PERFORMANCE_TIERS
_HEAVY_SERVICES = _config_support.HEAVY_SERVICES


def _load_config_data(config_file: Path, preserve: bool) -> dict:
    return _config_support.load_config_data(sys.modules[__name__], config_file, preserve)


def _apply_performance_tier(data: dict) -> None:
    _config_support.apply_performance_tier(sys.modules[__name__], data)


def _apply_vault_paths(data: dict, mnemos_vault: Path, raw_vault: Path) -> None:
    _config_support.apply_vault_paths(
        sys.modules[__name__], data, mnemos_vault, raw_vault
    )


def _apply_default_services(data: dict) -> None:
    _config_support.apply_default_services(data)


_LLM_PROVIDER_DEFAULTS = _config_support.LLM_PROVIDER_DEFAULTS
_MODEL_ENDPOINT_SPECS = _config_support.MODEL_ENDPOINT_SPECS


def _model_endpoint_spec(kind: str) -> dict:
    return _config_support.model_endpoint_spec(kind)


def _llm_provider_default(provider: str) -> dict:
    return _config_support.llm_provider_default(provider)


def _llm_cost_level(provider: str) -> str:
    return _config_support.llm_cost_level(provider)


def _infer_provider_from_base_url(base_url: str) -> str:
    return _config_support.infer_provider_from_base_url(base_url)


def _resolve_model_env_name(api_key_env: str) -> str:
    return _config_support.resolve_model_env_name(api_key_env)


def _resolve_base_url_env_name(api_key_env: str) -> str:
    return _config_support.resolve_base_url_env_name(api_key_env)


def _configure_llm_source(
    llm: dict,
    provider: str,
    api_key_source: str,
    *,
    api_key_env: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> None:
    _config_support.configure_llm_source(
        llm,
        provider,
        api_key_source,
        api_key_env=api_key_env,
        base_url=base_url,
        model=model,
    )


def _configure_model_endpoint(
    data: dict,
    kind: str,
    *,
    base_url: str,
    model: str,
    api_key_source: str,
    api_key_env: str = "",
) -> None:
    _config_support.configure_model_endpoint(
        data,
        kind,
        base_url=base_url,
        model=model,
        api_key_source=api_key_source,
        api_key_env=api_key_env,
    )


def _resolve_key_ref(api_key: str, api_key_source: str) -> tuple[str, str]:
    return _config_support.resolve_key_ref(api_key, api_key_source)


def _direct_model_config(data: dict, kind: str) -> Any:
    return _config_support.direct_model_config(data, kind)


def _resolve_required_model_configs(data: dict) -> dict[str, Any]:
    return _config_support.resolve_required_model_configs(data)


def _model_cfg_ready(cfg: Any) -> bool:
    return _config_support.model_cfg_ready(cfg)


def _smoke_model_endpoint(kind: str, cfg: Any) -> tuple[bool, str]:
    from scripts.verify_installation import (
        _smoke_exception_types,
        _smoke_embedding_api,
        _smoke_llm_api,
        _smoke_reranker_api,
    )

    smoke_fns = {
        "llm": _smoke_llm_api,
        "embedding": _smoke_embedding_api,
        "reranker": _smoke_reranker_api,
    }
    try:
        return smoke_fns[kind](cfg)
    except _smoke_exception_types() as e:
        logger.warning("Model endpoint smoke check failed for %s: %s", kind, e, exc_info=True)
        return False, str(e)


def _smoke_required_model_endpoints(
    data: dict, only: Set[str] | None = None
) -> tuple[bool, dict[str, str]]:
    return _config_support.smoke_required_model_endpoints(
        sys.modules[__name__],
        data,
        only,
    )


def _setup_llm_providers(llm: dict) -> None:
    _config_support.setup_llm_providers(llm)


def _reset_deployment_model_defaults(data: dict) -> None:
    """Remove built-in vendor/model defaults from fresh deployment prompts.

    Runtime defaults remain available elsewhere for backward compatibility, but
    setup must require users to explicitly provide model id, API URL, and key.
    """
    _config_support.reset_deployment_model_defaults(data)


def _apply_llm_env_overrides(llm: dict) -> None:
    _config_support.apply_llm_env_overrides(sys.modules[__name__], llm)


def _store_model_key_in_keyring(kind: str, api_key: str) -> str:
    ref = f"setup:{kind}"
    try:
        import keyring

        keyring.set_password("mnemos.llm", ref, api_key)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
    ) as e:
        raise SetupAbort(
            "无法安全写入系统 keyring。请先设置环境变量 "
            "MNEMOS_LLM_API_KEY / MNEMOS_EMBEDDING_API_KEY / MNEMOS_RERANKER_API_KEY "
            "/ MNEMOS_MULTIMODAL_API_KEY "
            "后重新运行 setup。"
        ) from e
    return f"keyring:{ref}"


def _print_required_model_env_help() -> None:
    print("  请为三类模型端点分别设置 model id、API 地址和 API key，例如：")
    for kind in ("llm", "embedding", "reranker"):
        spec = _MODEL_ENDPOINT_SPECS[kind]
        prefix = spec["env_prefix"]
        print(f"    # {spec['label']}")
        print(f"    export {prefix}_MODEL=your_model_id")
        print(f"    export {prefix}_BASE_URL=https://your-api.example/v1")
        print(f"    export {prefix}_API_KEY=your_key")
    optional = _OPTIONAL_MODEL_ENDPOINT_SPECS["multimodal"]
    optional_prefix = optional["env_prefix"]
    print("    # 可选：多模态模型，可跳过，不影响 Mnemos 正常使用")
    print(f"    export {optional_prefix}_MODEL=your_vision_model_id")
    print(f"    export {optional_prefix}_BASE_URL=https://your-vision-api.example/v1")
    print(f"    export {optional_prefix}_API_KEY=your_optional_key")


def _prompt_one_model_endpoint(data: dict, kind: str) -> None:
    spec = _MODEL_ENDPOINT_SPECS[kind]
    configs = _resolve_required_model_configs(data)
    existing = configs[kind]
    existing_ready = _model_cfg_ready(existing)
    default_base_url = getattr(existing, "base_url", "") or ""
    default_model = getattr(existing, "model", "") or ""

    print(f"\n  [必填] {spec['label']}")
    print("  需要填写：模型 ID、模型 API 地址、模型 API Key。")
    while True:
        base_url = ask(f"{spec['label']} API 地址(base_url):", default=str(default_base_url)).strip()
        model = ask(f"{spec['label']} 模型 ID:", default=str(default_model)).strip()
        if base_url and model:
            break
        print_err(f"{spec['label']} 必须填写 API 地址和模型 ID")
    api_key = getpass.getpass(f"  {spec['label']} API Key: ").strip()

    if api_key:
        source = _store_model_key_in_keyring(kind, api_key)
    elif existing_ready:
        source = str(getattr(existing, "source", "") or "")
        print_warn(f"{spec['label']} 复用已有 key 来源: {source}")
    else:
        print_err(f"{spec['label']} 未提供 API key")
        raise SetupAbort(f"{kind} API key is required")

    _configure_model_endpoint(
        data,
        kind,
        base_url=base_url,
        model=model,
        api_key_source=source,
    )


def _prompt_required_model_endpoints(
    data: dict,
    yes_mode: bool,
    max_smoke_attempts: int = DEFAULT_MAX_SMOKE_ATTEMPTS,
    interactive: bool | None = None,
) -> dict[str, object]:
    return _prompt_required_model_endpoints_impl(
        data,
        yes_mode=yes_mode,
        max_smoke_attempts=max_smoke_attempts,
        model_specs=_MODEL_ENDPOINT_SPECS,
        smoke_required_model_endpoints=_smoke_required_model_endpoints,
        prompt_one_model_endpoint=_prompt_one_model_endpoint,
        print_env_help=_print_required_model_env_help,
        ask=ask,
        print_ok=print_ok,
        print_err=print_err,
        print_warn=print_warn,
        interactive=interactive,
        abort_cls=RequiredModelEndpointSetupAbort,
    )


def _enable_semantic_search_from_env(embed: dict, reranker: dict, sf_key: str | None) -> None:
    _config_support.enable_semantic_search_from_env(
        sys.modules[__name__], embed, reranker, sf_key
    )


def _setup_semantic_search(data: dict, yes_mode: bool) -> None:
    _config_support.setup_semantic_search(sys.modules[__name__], data, yes_mode)


def _setup_optional_multimodal(data: dict, yes_mode: bool) -> None:
    setup_optional_multimodal(
        data,
        yes_mode,
        ask=ask,
        ask_yes_no=lambda prompt: ask_yes_no(prompt, default=False, yes_mode=False),
        configure_model_endpoint=_configure_model_endpoint,
        store_model_key_in_keyring=_store_model_key_in_keyring,
        print_ok=print_ok,
        print_warn=print_warn,
    )


def _write_config_file(config_file: Path, data: dict) -> None:
    tmp_file = config_file.with_suffix(".tmp")
    tmp_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    secure_file(tmp_file)
    os.replace(str(tmp_file), str(config_file))
    secure_file(config_file)
    print_ok(f"配置已写入: {config_file}")


def generate_config(
    mnemos_vault: Path,
    raw_vault: Path,
    yes_mode: bool = False,
    preserve: bool = False,
    max_smoke_attempts: int = DEFAULT_MAX_SMOKE_ATTEMPTS,
) -> Path:
    # 统一转为绝对路径，避免 daemon 从其他工作目录启动时解析错误
    mnemos_vault = mnemos_vault.expanduser().resolve()
    raw_vault = raw_vault.expanduser().resolve()

    config_file = _runtime_config_path()
    config_file.parent.mkdir(parents=True, exist_ok=True)

    if config_file.exists() and not yes_mode and not preserve:
        if not ask_yes_no(
            f"配置已存在: {config_file}，是否覆盖？", default=False, yes_mode=yes_mode
        ):
            print_warn("保留现有配置")
            return config_file

    data = _load_config_data(config_file, preserve)
    _apply_performance_tier(data)
    _apply_vault_paths(data, mnemos_vault, raw_vault)
    _apply_default_services(data)

    llm = data.setdefault("llm", {})
    _setup_llm_providers(llm)
    _reset_deployment_model_defaults(data)
    _apply_llm_env_overrides(llm)
    _setup_semantic_search(data, yes_mode)
    _setup_optional_multimodal(data, yes_mode)
    model_prompt_result = _prompt_required_model_endpoints(
        data,
        yes_mode,
        max_smoke_attempts=max_smoke_attempts,
    )
    if not model_prompt_result.get("verified", True):
        _write_config_file(config_file, data)
        action = str(model_prompt_result.get("action") or "unknown")
        errors = model_prompt_result.get("errors") or {}
        attempts_value = model_prompt_result.get("attempts", 1)
        attempts = attempts_value if isinstance(attempts_value, int) else int(str(attempts_value))
        if action == "dry_run_skip":
            print_warn("已保存当前配置并停止部署；请运行 dry-run 检查后重新执行 setup。")
        else:
            print_warn("已保存当前配置并停止部署；请修正模型端点后重新执行 setup。")
        raise RequiredModelEndpointSetupAbort(
            errors if isinstance(errors, Mapping) else {},
            attempts=attempts,
            user_action=action,
            config_path=str(config_file),
        )

    _write_config_file(config_file, data)
    return config_file


# ── 步骤 6: 初始化 Wiki 目录 ──


def init_wiki_structure(mnemos_vault: Path) -> None:
    """初始化主认知 Vault 的目录结构：L2 wiki + L2.4/L3/L4/L5 投影层。"""
    from core.setup.vault_layout import init_mnemos_vault, MNEMOS_VAULT_DIRS

    print(f"  初始化 Mnemos Vault 结构: {mnemos_vault}")
    init_mnemos_vault(mnemos_vault)
    print_ok(f"Mnemos Vault 结构已就绪 ({len(MNEMOS_VAULT_DIRS)} 个子目录)")


def init_vaults(mnemos_vault: Path, raw_vault: Path) -> None:
    """创建两个 Vault 的 .obsidian 标记目录。"""
    print(f"  初始化 Vault 标记: {mnemos_vault}, {raw_vault}")
    (mnemos_vault / ".obsidian").mkdir(parents=True, exist_ok=True)
    (raw_vault / ".obsidian").mkdir(parents=True, exist_ok=True)
    print_ok("Vault 标记已创建")


def register_vaults(mnemos_vault: Path, raw_vault: Path) -> None:
    """将两个 Vault 注册到 Obsidian。"""
    print("  注册 Vault 到 Obsidian...")
    try:
        from integrations.backends.obsidian_backend import ensure_vault_recognized

        ensure_vault_recognized(mnemos_vault)
        ensure_vault_recognized(raw_vault)
        print_ok("Vault 已注册到 Obsidian")
    except SETUP_OPERATION_ERRORS as e:
        print_warn(f"注册 Obsidian Vault 失败（可稍后手动打开）: {e}")


# ── 步骤 7: 安装 Agent Hooks ──


def install_agent_hooks(yes_mode: bool = False) -> bool:
    print("  安装 AI Agent 主动接入（hooks + MCP + 主动策略）...")
    try:
        from core.cli.commands import mcp as mcp_cmd
        from integrations.olympus import AgentRegistry

        agents = AgentRegistry.discover_all()
        installed = 0
        active = 0
        for agent in agents:
            try:
                hooks_ok = agent.install_hooks()
                mcp_ok = agent.install_mcp_server()
                policy_ok = agent.install_active_policy()
                installed += 1 if hooks_ok else 0
                active += 1 if (hooks_ok and mcp_ok and policy_ok) else 0
                print(f"    {'✓' if hooks_ok else '✗'} {agent.name} hooks")
                print(f"    {'✓' if mcp_ok else '✗'} {agent.name} MCP 主动工具")
                print(f"    {'✓' if policy_ok else '✗'} {agent.name} 主动使用策略")
            except SETUP_OPERATION_ERRORS as e:
                print(f"    ✗ {agent.name} 主动接入: {e}")
        mcp_only_active = 0
        for name in sorted(mcp_cmd._MCP_ONLY_AGENTS):
            print(f"    安装 {name} MCP-only 主动接入...")
            if mcp_cmd._install_mcp_only_agent(name):
                mcp_only_active += 1
        total_targets = len(agents) + len(mcp_cmd._MCP_ONLY_AGENTS)
        print_ok(f"Agent hooks 安装完成: {installed}/{len(agents)}")
        print_ok(f"Agent 主动接入完成: {active + mcp_only_active}/{total_targets}")
        return active + mcp_only_active > 0
    except SETUP_OPERATION_ERRORS as e:
        print_err(f"Agent 主动接入安装失败: {e}")
        return False


# ── 步骤 7.5: 配置自动蒸馏 ──


def _detect_installed_agents() -> Dict[str, bool]:
    """检测本地安装的 AI Agent"""
    agents = {}
    # Kimi
    agents["kimi"] = shutil.which("kimi") is not None
    # Claude
    agents["claude"] = shutil.which("claude") is not None or (Path.home() / ".claude").exists()
    # Cursor
    agents["cursor"] = (Path.home() / ".cursor").exists() or shutil.which("cursor") is not None
    # Windsurf
    agents["windsurf"] = (Path.home() / ".windsurf").exists() or shutil.which("windsurf") is not None
    return agents


def setup_distillation(yes_mode: bool = False) -> str:
    """配置自动蒸馏方案。

    Returns:
        用户选择的方案标识: "api"
    """
    print("  检测自动蒸馏环境...")
    agents = _detect_installed_agents()
    apis = _detect_api_configs()

    # 显示检测结果
    print("  检测到以下 AI Agent:")
    for name, available in agents.items():
        icon = "✓" if available else "✗"
        print(f"    [{icon}] {name.title()}")

    print("  检测到 API 配置:")
    has_any_api = False
    for name, available in apis.items():
        icon = "✓" if available else "✗"
        print(f"    [{icon}] {name.title()} API")
        if available:
            has_any_api = True

    if not has_any_api:
        print_err("未检测到 LLM API key，自动蒸馏无法启用；安装应在配置生成阶段停止。")
        raise SetupAbort("LLM API key is required")

    print("  自动选择: API 蒸馏模式")
    _write_distill_config("api")
    return "api"


def _write_distill_config(strategy: str):
    """将蒸馏策略写入运行时配置"""
    try:
        config_file = _runtime_config_path()
        config_kind = inspect_path_kind(config_file)
        if config_kind == "file":
            data = json.loads(read_native_bytes(config_file).decode("utf-8"))
        elif config_kind == "missing":
            data = {}
        else:
            raise DurableIOError("runtime_config_path_not_regular")
        data.setdefault("distill", {})
        data["distill"]["strategy"] = strategy
        data["distill"]["auto_enabled"] = strategy != "generic"
        secure_atomic_write_bytes(
            config_file.parent,
            config_file.name,
            (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        secure_file(config_file)
    except SETUP_OPERATION_ERRORS as e:
        print_warn(f"写入蒸馏配置失败: {e}")


# ── 步骤 8: 启动 Daemon ──


def start_daemon(yes_mode: bool = False) -> bool:
    print("  启动 Mnemos 守护进程...")
    # 先检查是否已在运行，使用与 daemon 一致的 database_dir
    try:
        from core.config import get_config

        database_dir = get_config().database_dir
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        subprocess.SubprocessError
    ):
        database_dir = _mnemos_dir()
    pid_file = database_dir / "daemon.pid"
    try:
        pid_kind = inspect_path_kind(pid_file)
        if pid_kind == "missing":
            pid = 0
        elif pid_kind == "file":
            pid = int(read_native_bytes(pid_file).decode("utf-8").strip())
        else:
            raise DurableIOError("daemon_pid_path_not_regular")
        if pid > 0:
            os.kill(pid, 0)  # 检查进程是否存在
            print_ok(f"Daemon 已在运行 (PID: {pid})")
            return True
    except (OSError, UnicodeError, ValueError) as exc:
        print_warn(f"Daemon 状态不可验证，拒绝重复启动: {exc}")
        return False

    if not ask_yes_no("是否启动守护进程？", default=True, yes_mode=yes_mode):
        print_warn("跳过 daemon 启动")
        return True

    result = subprocess.run(
        [_PYTHON_EXE, str(PROJECT_ROOT / "mnemos_daemon.py"), "start"],
        capture_output=True,
        text=True,
        timeout=15,  # [P1-FIX] 防止 daemon 启动挂起
    )
    if result.returncode == 0:
        print_ok("Daemon 已启动")
        return True
    print_err(f"Daemon 启动失败: {result.stderr[:200]}")
    return False


# ── 步骤 9: 配置定时任务 ──


def setup_scheduler(yes_mode: bool = False) -> bool:
    print("  配置系统定时任务...")
    system = platform.system()

    if system == "Darwin":
        return _setup_macos_scheduler(yes_mode)
    elif system == "Linux":
        return _setup_linux_scheduler(yes_mode)
    elif system == "Windows":
        return _setup_windows_scheduler()
    else:
        print_warn(f"未知平台 {system}，跳过定时任务配置")
        return True


def _setup_windows_scheduler() -> bool:
    cli_path = PROJECT_ROOT / "mnemos_cli.py"
    cmd = [_PYTHON_EXE, str(cli_path), "scheduler", "install-windows"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print_warn(f"Windows Task Scheduler 自动配置失败: {exc}")
        print(f'    手动运行: "{_PYTHON_EXE}" "{cli_path}" scheduler install-windows')
        return False

    if result.returncode == 0:
        print_ok("Windows Task Scheduler 已配置")
        if result.stdout.strip():
            print(result.stdout.strip())
        return True

    details = (result.stderr or result.stdout or "").strip()
    if details:
        print_warn(f"Windows Task Scheduler 自动配置失败: {details[:300]}")
    else:
        print_warn("Windows Task Scheduler 自动配置失败")
    print(f'    手动运行: "{_PYTHON_EXE}" "{cli_path}" scheduler install-windows')
    return False


def _setup_macos_scheduler(yes_mode: bool = False) -> bool:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.mnemos.daemon.plist"
    if plist_path.exists() and not yes_mode:
        if not ask_yes_no(
            f"launchd 配置已存在: {plist_path}，是否覆盖？", default=False, yes_mode=yes_mode
        ):
            print_warn("保留现有 launchd 配置")
            return True

    def _xml_esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    exe = _xml_esc(str(_PYTHON_EXE))
    script = _xml_esc(str(PROJECT_ROOT / "mnemos_daemon.py"))
    log_out = _xml_esc(str(_mnemos_dir() / "logs" / "daemon.launchd.log"))
    log_err = _xml_esc(str(_mnemos_dir() / "logs" / "daemon.launchd.err"))

    # 只写入非空环境变量，避免空字符串覆盖用户 shell 中已导出的值
    env_vars = {
        "MNEMOS_DIR": str(_mnemos_dir()),
        "MNEMOS_LLM_API_KEY": os.environ.get("MNEMOS_LLM_API_KEY", ""),
        "MNEMOS_LLM_BASE_URL": os.environ.get("MNEMOS_LLM_BASE_URL", ""),
        "MNEMOS_LLM_MODEL": os.environ.get("MNEMOS_LLM_MODEL", ""),
        "MNEMOS_EMBEDDING_API_KEY": os.environ.get("MNEMOS_EMBEDDING_API_KEY", ""),
        "MNEMOS_EMBEDDING_BASE_URL": os.environ.get("MNEMOS_EMBEDDING_BASE_URL", ""),
        "MNEMOS_EMBEDDING_MODEL": os.environ.get("MNEMOS_EMBEDDING_MODEL", ""),
        "MNEMOS_RERANKER_API_KEY": os.environ.get("MNEMOS_RERANKER_API_KEY", ""),
        "MNEMOS_RERANKER_BASE_URL": os.environ.get("MNEMOS_RERANKER_BASE_URL", ""),
        "MNEMOS_RERANKER_MODEL": os.environ.get("MNEMOS_RERANKER_MODEL", ""),
        "MNEMOS_MULTIMODAL_API_KEY": os.environ.get("MNEMOS_MULTIMODAL_API_KEY", ""),
        "MNEMOS_MULTIMODAL_BASE_URL": os.environ.get("MNEMOS_MULTIMODAL_BASE_URL", ""),
        "MNEMOS_MULTIMODAL_MODEL": os.environ.get("MNEMOS_MULTIMODAL_MODEL", ""),
        "SILICONFLOW_API_KEY": os.environ.get("SILICONFLOW_API_KEY", ""),
        "SILICONFLOW_BASE_URL": os.environ.get("SILICONFLOW_BASE_URL", ""),
        "SILICONFLOW_MODEL": os.environ.get("SILICONFLOW_MODEL", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", ""),
        "DMXAPI_API_KEY": os.environ.get("DMXAPI_API_KEY", ""),
        "DMX_API_KEY": os.environ.get("DMX_API_KEY", ""),
        "DMXAPI_BASE_URL": os.environ.get("DMXAPI_BASE_URL", ""),
        "DMXAPI_MODEL": os.environ.get("DMXAPI_MODEL", ""),
    }
    env_vars = {k: v for k, v in env_vars.items() if v}
    env_xml = "\n".join(
        f"        <key>{_xml_esc(k)}</key>\n        <string>{_xml_esc(v)}</string>"
        for k, v in env_vars.items()
    )
    env_block = ""
    if env_xml:
        env_block = f"""    <key>EnvironmentVariables</key>
    <dict>
{env_xml}
    </dict>
"""

    # launchd 管理前台进程，因此使用 run；配合 KeepAlive 实现保活
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mnemos.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
        <string>{script}</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
{env_block}</dict>
</plist>
"""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = _mnemos_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    secure_directory(log_dir)
    plist_path.write_text(plist_content, encoding="utf-8")

    # 加载: 先 unload 再 load，避免重复加载报错
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)], capture_output=True, text=True
    )
    if result.returncode == 0:
        print_ok(f"macOS launchd 已配置: {plist_path}")
        return True
    # launchctl 在较新 macOS 上可能需要 domain/user 参数
    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True
    )
    if result.returncode == 0:
        print_ok(f"macOS launchd 已配置: {plist_path}")
        return True
    print_warn(f"launchctl load 返回非零: {result.stderr[:200]}")
    return False


def _setup_linux_scheduler(yes_mode: bool = False) -> bool:
    log_file = _mnemos_dir() / "logs" / "daemon.cron.log"
    cron_cmd = f"cd {PROJECT_ROOT} && {_PYTHON_EXE} -m mnemos_daemon start >> {log_file} 2>&1"

    # 非空环境变量放在 crontab 顶部，避免行内转义问题
    env_lines = []
    for k, v in {
        "MNEMOS_DIR": str(_mnemos_dir()),
        "MNEMOS_LLM_API_KEY": os.environ.get("MNEMOS_LLM_API_KEY", ""),
        "MNEMOS_LLM_BASE_URL": os.environ.get("MNEMOS_LLM_BASE_URL", ""),
        "MNEMOS_LLM_MODEL": os.environ.get("MNEMOS_LLM_MODEL", ""),
        "MNEMOS_EMBEDDING_API_KEY": os.environ.get("MNEMOS_EMBEDDING_API_KEY", ""),
        "MNEMOS_EMBEDDING_BASE_URL": os.environ.get("MNEMOS_EMBEDDING_BASE_URL", ""),
        "MNEMOS_EMBEDDING_MODEL": os.environ.get("MNEMOS_EMBEDDING_MODEL", ""),
        "MNEMOS_RERANKER_API_KEY": os.environ.get("MNEMOS_RERANKER_API_KEY", ""),
        "MNEMOS_RERANKER_BASE_URL": os.environ.get("MNEMOS_RERANKER_BASE_URL", ""),
        "MNEMOS_RERANKER_MODEL": os.environ.get("MNEMOS_RERANKER_MODEL", ""),
        "MNEMOS_MULTIMODAL_API_KEY": os.environ.get("MNEMOS_MULTIMODAL_API_KEY", ""),
        "MNEMOS_MULTIMODAL_BASE_URL": os.environ.get("MNEMOS_MULTIMODAL_BASE_URL", ""),
        "MNEMOS_MULTIMODAL_MODEL": os.environ.get("MNEMOS_MULTIMODAL_MODEL", ""),
        "SILICONFLOW_API_KEY": os.environ.get("SILICONFLOW_API_KEY", ""),
        "SILICONFLOW_BASE_URL": os.environ.get("SILICONFLOW_BASE_URL", ""),
        "SILICONFLOW_MODEL": os.environ.get("SILICONFLOW_MODEL", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", ""),
        "DMXAPI_API_KEY": os.environ.get("DMXAPI_API_KEY", ""),
        "DMX_API_KEY": os.environ.get("DMX_API_KEY", ""),
        "DMXAPI_BASE_URL": os.environ.get("DMXAPI_BASE_URL", ""),
        "DMXAPI_MODEL": os.environ.get("DMXAPI_MODEL", ""),
    }.items():
        if v:
            env_lines.append(f"{k}={v}")

    cron_entry = cron_cmd
    if env_lines:
        cron_entry = "\n".join(env_lines) + "\n" + cron_cmd

    print_warn("Linux 定时任务请手动配置 cron（不会覆盖现有 crontab）：")
    print("    (crontab -l 2>/dev/null; cat <<'CRON_EOF' | crontab -")
    print(cron_entry)
    print("CRON_EOF)")
    return True


# ── 主流程 ──


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(description="Mnemos 自动部署脚本")
    parser.add_argument("--yes", "-y", action="store_true", help="全自动模式，不提示")
    parser.add_argument("--skip-backend", action="store_true", help="跳过 Raw Vault 后端确认")
    parser.add_argument("--skip-daemon", action="store_true", help="跳过启动守护进程")
    parser.add_argument("--skip-scheduler", action="store_true", help="跳过配置系统定时任务")
    parser.add_argument("--skip-hooks", action="store_true", help="跳过安装 Agent Hooks")
    parser.add_argument(
        "--dry-run", action="store_true", help="只检查环境，不执行任何安装/启动操作"
    )
    parser.add_argument("--json", action="store_true", help="JSON 输出（目前用于 dry-run）")
    parser.add_argument("--skip-verify", action="store_true", help="跳过部署后验证")
    parser.add_argument("--skip-backfill", action="store_true", help="跳过历史数据回填")
    parser.add_argument("--skip-e2e", action="store_true", help="跳过 E2E 全链路探针")
    parser.add_argument(
        "--preserve-config", action="store_true", help="保留现有配置，仅更新必要字段（适合重装）"
    )
    parser.add_argument(
        "--max-smoke-attempts",
        type=_positive_int,
        default=DEFAULT_MAX_SMOKE_ATTEMPTS,
        help="必填模型端点 smoke 最大尝试次数（默认 3）",
    )
    parser.add_argument("--venv-reexec", action="store_true", help=argparse.SUPPRESS)
    return parser


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer >= 1") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return parsed


def _normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    """非交互式终端未传 --yes 时强制进入 dry-run 模式。"""
    if not sys.stdin.isatty() and not args.yes:
        if not args.json:
            print("检测到非交互式终端，未传入 --yes，仅执行 dry-run 检查")
        args.dry_run = True
    return args


def _run_dry_run(json_output: bool) -> None:
    """执行 dry-run 检查并退出。"""
    if json_output:
        print(json.dumps(collect_dry_run_status(), ensure_ascii=False, indent=2))
        sys.exit(0)

    print("=" * 60)
    print("Mnemos 部署检查 (dry-run)")
    print("=" * 60)
    print(f"项目路径: {PROJECT_ROOT}")
    print(f"平台: {platform.system()} {platform.release()}")
    print(f"Python: {sys.version.split()[0]}")
    print()
    check_python()
    print()
    installed, version, app_path = detect_obsidian_app()
    if installed:
        ver_str = f" v{version}" if version else ""
        print_ok(f"Obsidian 已安装{ver_str} ({app_path})")
    else:
        print_warn("未检测到 Obsidian 应用")
        print("  请从 https://obsidian.md/download 下载安装")
    vaults = find_obsidian_vaults()
    if vaults:
        print_ok(f"发现 {len(vaults)} 个 Vault")
    else:
        print_warn("未检测到 Obsidian Vault")
    print()
    print("dry-run 完成，未执行任何安装或启动操作。")
    print("如需实际部署，请去掉 --dry-run 参数。")
    sys.exit(0)


def _step_check_python() -> bool:
    print_step(1, 13, "检查 Python 版本")
    return check_python()


def _step_install_deps(yes_mode, venv_reexec, reexec_args=None, reexec_entrypoint=None) -> bool:
    print_step(2, 13, "安装依赖")
    if venv_reexec:
        print_ok("已在虚拟环境中，跳过依赖安装")
        return True
    if install_dependencies(
        yes_mode=yes_mode, reexec_args=reexec_args, reexec_entrypoint=reexec_entrypoint
    ):
        return True
    return ask_yes_no("依赖安装失败，是否继续？", default=False, yes_mode=yes_mode)


def _step_setup_obsidian(yes_mode: bool) -> tuple[bool, Path, Path]:
    print_step(4, 13, "检测 Obsidian")
    obsidian_ok, mnemos_vault, raw_vault = setup_obsidian(yes_mode=yes_mode)
    if not mnemos_vault:
        mnemos_vault = DEFAULT_MNEMOS_VAULT
    if not raw_vault:
        raw_vault = DEFAULT_RAW_VAULT
    return obsidian_ok, mnemos_vault, raw_vault


def _step_generate_config(
    mnemos_vault: Path,
    raw_vault: Path,
    yes_mode: bool,
    preserve: bool,
    max_smoke_attempts: int = DEFAULT_MAX_SMOKE_ATTEMPTS,
) -> Path:
    print_step(5, 13, "生成配置")
    return generate_config(
        mnemos_vault,
        raw_vault,
        yes_mode=yes_mode,
        preserve=preserve,
        max_smoke_attempts=max_smoke_attempts,
    )


def _step_init_vaults(mnemos_vault: Path, raw_vault: Path) -> None:
    print_step(6, 13, "初始化 Vault")
    init_vaults(mnemos_vault, raw_vault)
    init_wiki_structure(mnemos_vault)
    register_vaults(mnemos_vault, raw_vault)


def _step_hooks(yes_mode: bool, skip: bool, step: int, total: int) -> None:
    print_step(step, total, "安装 Agent Hooks")
    if skip:
        print_warn("已跳过 (--skip-hooks)")
        return
    if not install_agent_hooks(yes_mode=yes_mode):
        raise SetupAbort("Agent hooks / MCP / Active Policy installation failed")


def _step_distillation(yes_mode: bool, step: int, total: int) -> str:
    print_step(step, total, "配置自动蒸馏")
    return setup_distillation(yes_mode=yes_mode)


def _step_daemon(yes_mode: bool, skip: bool, step: int, total: int) -> None:
    print_step(step, total, "启动守护进程")
    if skip:
        print_warn("已跳过 (--skip-daemon)")
        return
    if not start_daemon(yes_mode=yes_mode):
        raise SetupAbort("daemon startup failed")


def _step_scheduler(yes_mode: bool, skip: bool, step: int, total: int) -> None:
    print_step(step, total, "配置定时任务")
    if skip:
        print_warn("已跳过 (--skip-scheduler)")
        return
    if not setup_scheduler(yes_mode=yes_mode):
        raise SetupAbort("scheduler setup failed")


def _step_verify(skip: bool, step: int, total: int, yes_mode: bool = False) -> None:
    print_step(step, total, "验证部署")
    if skip:
        print_warn("已跳过 (--skip-verify)")
        return
    try:
        import subprocess

        r = subprocess.run(
            [_PYTHON_EXE, str(PROJECT_ROOT / "scripts" / "verify_installation.py")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode == 0:
            print_ok("部署验证通过")
        else:
            print_err("部署验证未通过")
            if r.stdout.strip():
                print(r.stdout.strip())
            if r.stderr.strip():
                print(r.stderr.strip())
            raise SetupAbort("deployment verification failed")
    except SetupAbort:
        raise
    except SETUP_OPERATION_ERRORS as e:
        print_warn(f"部署验证运行失败: {e}")
        if yes_mode:
            raise SetupAbort(f"deployment verification failed: {e}") from e


def _step_backfill(skip: bool, step: int, total: int) -> bool:
    print_step(step, total, "安排历史数据回填")
    if skip:
        return False
    backfill_started = _schedule_backfill_background()
    if backfill_started:
        print_ok("历史回填已在后台启动（低优先级）")
    else:
        print_warn("历史回填启动失败，请稍后手动运行")
    return backfill_started


def _step_e2e(skip: bool, step: int, total: int) -> None:
    print_step(step, total, "运行 E2E 全链路探针")
    if skip:
        print_warn("已跳过 (--skip-e2e)")
        return
    try:
        import subprocess

        e2e_cmd = [_PYTHON_EXE, str(PROJECT_ROOT / "scripts" / "e2e_probe.py"), "--dry-run", "--no-api"]
        r = subprocess.run(
            e2e_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode == 0:
            print_ok("E2E 探针通过")
        else:
            print_err("E2E 探针未通过")
            if r.stdout.strip():
                print(r.stdout.strip())
            if r.stderr.strip():
                print(r.stderr.strip())
            raise SetupAbort("E2E probe failed")
    except SetupAbort:
        raise
    except SETUP_OPERATION_ERRORS as e:
        raise SetupAbort(f"E2E probe failed: {e}") from e


def _print_header(yes_mode: bool) -> None:
    print("=" * 60)
    print("Mnemos 自动部署")
    print("=" * 60)
    print(f"项目路径: {PROJECT_ROOT}")
    print(f"平台: {platform.system()} {platform.release()}")
    print(f"Python: {sys.version.split()[0]}")
    if yes_mode:
        print("模式: 全自动 (--yes)")


def _run_setup(args: argparse.Namespace) -> None:
    """Execute the full interactive/automatic setup flow."""
    yes_mode = args.yes
    total_steps = 13

    _print_header(yes_mode)

    if not _step_check_python():
        sys.exit(1)

    if not _step_install_deps(
        yes_mode=yes_mode,
        venv_reexec=args.venv_reexec,
        reexec_args=getattr(args, "reexec_args", None),
        reexec_entrypoint=getattr(args, "reexec_entrypoint", None),
    ):
        raise SetupAbort("dependency installation failed")

    print_step(3, total_steps, "检测 Raw Vault 后端")
    storage_ok = setup_l1_backend(skip=args.skip_backend, yes_mode=yes_mode)

    obsidian_ok, mnemos_vault, raw_vault = _step_setup_obsidian(yes_mode=yes_mode)
    if not obsidian_ok:
        sys.exit(1)

    _step_generate_config(
        mnemos_vault,
        raw_vault,
        yes_mode=yes_mode,
        preserve=args.preserve_config,
        max_smoke_attempts=getattr(
            args, "max_smoke_attempts", DEFAULT_MAX_SMOKE_ATTEMPTS
        ),
    )
    _step_init_vaults(mnemos_vault, raw_vault)
    _step_hooks(yes_mode=yes_mode, skip=args.skip_hooks, step=7, total=total_steps)
    distill_strategy = _step_distillation(yes_mode=yes_mode, step=8, total=total_steps)
    _step_daemon(yes_mode=yes_mode, skip=args.skip_daemon, step=9, total=total_steps)
    _step_scheduler(yes_mode=yes_mode, skip=args.skip_scheduler, step=10, total=total_steps)
    _step_verify(skip=args.skip_verify, step=11, total=total_steps, yes_mode=yes_mode)

    backfill_started = False
    if storage_ok and not args.skip_backfill:
        backfill_started = _step_backfill(skip=False, step=12, total=total_steps)

    if not args.skip_e2e:
        _step_e2e(skip=False, step=13, total=total_steps)

    _print_completion_summary(mnemos_vault, raw_vault, distill_strategy, backfill_started)


def main():
    parser = _build_parser()
    args = parser.parse_args()
    args = _normalize_args(args)

    if args.dry_run:
        _run_dry_run(json_output=args.json)

    try:
        _run_setup(args)
    except SetupAbort as e:
        print_err(str(e))
        sys.exit(1)
    sys.exit(0)


def _schedule_backfill_background() -> bool:
    """在后台启动历史回填任务。"""
    try:
        import subprocess

        subprocess.Popen(
            [
                _PYTHON_EXE,
                "-m",
                "mnemos_cli",
                "sync",
                "backfill",
                "--source",
                "all",
                "--since",
                "0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(platform.system() != "Windows"),
        )
        return True
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        subprocess.SubprocessError
    ):
        return False


def _print_completion_summary(
    mnemos_vault: Path,
    raw_vault: Path,
    distill_strategy: str,
    backfill_started: bool = False,
):
    """打印部署完成摘要，并提示历史回填状态。"""
    print("\n" + "=" * 60)
    print("Mnemos 部署完成")
    print("=" * 60)
    print(f"  Mnemos Vault: {mnemos_vault}")
    print(f"  raw Vault: {raw_vault}")

    # [P2-3] 友好的蒸馏策略显示
    strategy_labels = {
        "api": "API 自动蒸馏（已配置）",
    }
    label = strategy_labels.get(distill_strategy, distill_strategy)
    print(f"  蒸馏策略: {label}")

    print()
    print("  常用命令:")
    print("    mnemos doctor          — 系统诊断")
    print("    mnemos status          — 查看运行状态")
    print("    mnemos daemon start    — 启动守护进程")
    print()
    if backfill_started:
        print("  历史全量回填: 已在后台自动启动（低优先级）")
        print("    查看进度: mnemos sync audit")
        print("    查看状态: mnemos status")
    else:
        print("  历史全量回填（可选，低优先级后台运行）:")
        print("    python3 -m mnemos_cli sync backfill --source all --since 0")
    print()
    print("  更多帮助: https://github.com/jielumaizui/mnemos")
    print("=" * 60)


if __name__ == "__main__":
    main()
