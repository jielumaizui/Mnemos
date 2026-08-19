"""Bounded required model endpoint prompt loop for setup."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from typing import Any, NoReturn


REQUIRED_MODEL_ENDPOINT_FAILURE_CODE = "required_model_endpoints_failed"
DEFAULT_MAX_SMOKE_ATTEMPTS = 3


class RequiredModelEndpointSetupAbort(RuntimeError):
    """Raised when required model endpoint setup cannot continue."""

    failure_code = REQUIRED_MODEL_ENDPOINT_FAILURE_CODE

    def __init__(
        self,
        errors: Mapping[str, str],
        *,
        attempts: int,
        user_action: str,
        config_path: str = "",
        message: str = "required model endpoints are missing or unreachable",
    ) -> None:
        super().__init__(message)
        self.errors = dict(errors)
        self.attempts = attempts
        self.user_action = user_action
        self.config_path = config_path

    def to_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "required_model_endpoints_failed": True,
            "required_model_endpoint_errors": dict(self.errors),
            "required_model_endpoint_attempts": self.attempts,
            "required_model_endpoint_action": self.user_action,
        }
        if self.config_path:
            metadata["required_model_endpoint_config_path"] = self.config_path
        return metadata


def _print_required_model_errors(
    errors: Mapping[str, str],
    *,
    model_specs: Mapping[str, Mapping[str, str]],
) -> None:
    for kind, error in errors.items():
        print(f"  - {model_specs[kind]['label']}: {error}")


def _choose_failure_action(
    errors: Mapping[str, str],
    *,
    attempts: int,
    max_smoke_attempts: int,
    model_specs: Mapping[str, Mapping[str, str]],
    ask: Callable[..., str],
    print_warn: Callable[[str], None],
) -> str:
    _print_required_model_errors(errors, model_specs=model_specs)
    print(f"  smoke 尝试 {attempts}/{max_smoke_attempts} 未通过。")
    choice = ask(
        "处理方式 [r=重试, s=保存配置并退出, e=打印环境变量示例, d=dry-run 跳过 smoke]:",
        default="r",
    ).strip().lower()
    aliases = {
        "r": "retry",
        "retry": "retry",
        "s": "save_and_exit",
        "save": "save_and_exit",
        "save-exit": "save_and_exit",
        "e": "env_help",
        "env": "env_help",
        "d": "dry_run_skip",
        "dry-run": "dry_run_skip",
        "dry_run": "dry_run_skip",
    }
    action = aliases.get(choice)
    if action is None:
        print_warn(f"未知处理方式 {choice!r}，默认重试。")
        return "retry"
    return action


def _abort_required_model_setup(
    errors: Mapping[str, str],
    *,
    attempts: int,
    user_action: str,
    abort_cls: type[RequiredModelEndpointSetupAbort] = RequiredModelEndpointSetupAbort,
    message: str = "required model endpoints are missing or unreachable",
) -> NoReturn:
    raise abort_cls(
        errors,
        attempts=attempts,
        user_action=user_action,
        message=message,
    )


def prompt_required_model_endpoints(
    data: dict[str, Any],
    *,
    yes_mode: bool,
    max_smoke_attempts: int,
    model_specs: Mapping[str, Mapping[str, str]],
    smoke_required_model_endpoints: Callable[[dict[str, Any]], tuple[bool, dict[str, str]]],
    prompt_one_model_endpoint: Callable[[dict[str, Any], str], None],
    print_env_help: Callable[[], None],
    ask: Callable[..., str],
    print_ok: Callable[[str], None],
    print_err: Callable[[str], None],
    print_warn: Callable[[str], None],
    interactive: bool | None = None,
    abort_cls: type[RequiredModelEndpointSetupAbort] = RequiredModelEndpointSetupAbort,
) -> dict[str, object]:
    if max_smoke_attempts < 1:
        raise ValueError("max_smoke_attempts must be >= 1")

    ok, errors = smoke_required_model_endpoints(data)
    attempts = 1
    if ok:
        print_ok("LLM / Embedding / Reranker 三类模型端点均已通过 smoke 验证")
        return {"verified": True, "attempts": attempts, "errors": {}}

    msg = "Mnemos 新部署必须配置并验证 LLM、Embedding、Reranker 三类模型端点。"
    if interactive is None:
        interactive = sys.stdin.isatty()
    if yes_mode or not interactive:
        print_err(msg)
        _print_required_model_errors(errors, model_specs=model_specs)
        print_env_help()
        _abort_required_model_setup(
            errors,
            attempts=attempts,
            user_action="yes_mode" if yes_mode else "non_interactive",
            abort_cls=abort_cls,
        )

    print("\n  [必填] 模型端点配置")
    print("  " + msg)
    print("  不需要选择厂商；只要端点兼容对应 API，填写模型 ID、API 地址和 key 即可。")
    while True:
        action = _choose_failure_action(
            errors,
            attempts=attempts,
            max_smoke_attempts=max_smoke_attempts,
            model_specs=model_specs,
            ask=ask,
            print_warn=print_warn,
        )
        if action == "env_help":
            print_env_help()
            continue
        if action in {"save_and_exit", "dry_run_skip"}:
            return {
                "verified": False,
                "attempts": attempts,
                "errors": dict(errors),
                "action": action,
            }
        if attempts >= max_smoke_attempts:
            break
        for kind in ("llm", "embedding", "reranker"):
            if kind in errors:
                prompt_one_model_endpoint(data, kind)
        attempts += 1
        ok, errors = smoke_required_model_endpoints(data)
        if ok:
            print_ok("三类模型端点均已通过验证")
            return {"verified": True, "attempts": attempts, "errors": {}}
        if attempts >= max_smoke_attempts:
            break
        print_warn("仍有模型端点不可用，请重新填写失败项。")
    _abort_required_model_setup(
        errors,
        attempts=attempts,
        user_action="max_smoke_attempts",
        abort_cls=abort_cls,
    )
