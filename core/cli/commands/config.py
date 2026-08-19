"""Config command for Mnemos CLI."""

import logging


from core.config import Config
from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def cmd_config(args):
    """查看/编辑配置"""
    config = _get_config()
    if args.set:
        key, val = args.set.split("=", 1)
        config.set(key, Config.auto_type(val))
        config.save()
        print(f"✓ 已设置 {key} = {Config.auto_type(val)}")
    else:
        import yaml
        import copy

        safe_config = copy.deepcopy(config.to_dict())
        # 脱敏敏感字段
        for section in ["l1_storage", "llm", "embedding"]:
            if section in safe_config and isinstance(safe_config[section], dict):
                if safe_config[section].get("token"):
                    safe_config[section]["token"] = "***"
                if safe_config[section].get("api_key"):
                    safe_config[section]["api_key"] = "***"
                providers = safe_config[section].get("providers")
                if isinstance(providers, dict):
                    for provider_cfg in providers.values():
                        if isinstance(provider_cfg, dict) and provider_cfg.get("api_key"):
                            provider_cfg["api_key"] = "***"
        print(yaml.dump(safe_config, allow_unicode=True, sort_keys=False))
