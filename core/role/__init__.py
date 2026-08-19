"""
Role Graph — 当前激活的"自我"

用于 Reflection Router：根据当前激活角色选择 Reflection Capability 配置。
"""

from core.role.role_graph import Role, RoleActivation, RoleGraph

__all__ = [
    "Role",
    "RoleActivation",
    "RoleGraph",
]
