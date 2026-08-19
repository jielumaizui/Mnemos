"""
Reflection Router — 轻量 role + scene 路由

职责：
- 根据 query + 近期上下文推断 role
- 根据 query 分类 scene
- 决定是否触发 Reflection Capability

规则：
- scene == "default" 不触发 Reflection
- 其他 scene 返回路由结果，供上层调用 ReflectionCapability.reflect()
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from core.role.role_graph import Role, RoleActivation, RoleGraph


@dataclass
class ReflectionRoute:
    """Reflection 路由结果"""

    should_reflect: bool
    scene: str
    role: Role
    role_activation: RoleActivation
    reason: str

    def to_dict(self):
        return {
            "should_reflect": self.should_reflect,
            "scene": self.scene,
            "role": self.role.value,
            "role_confidence": self.role_activation.confidence,
            "reason": self.reason,
        }


class ReflectionRouter:
    """轻量 Reflection 路由器"""

    SCENES = ["new_project", "major_decision", "role_shift", "repeated_stuck", "default"]

    # scene 关键词信号
    SCENE_SIGNALS = {
        "new_project": [
            r"(?:启动|开始|新建|立项|新做|想做|准备做|开个新|开始搞|重新做|重构|重写)",
            r"\b(?:start|launch|new project|rebuild|refactor from scratch)\b",
        ],
        "major_decision": [
            r"(?:决定|决策|选择|权衡|取舍|重大|要不要|是否|纠结|犹豫)",
            r"\b(?:decide|decision|choose|trade-off|whether to|should i)\b",
        ],
        "role_shift": [
            r"(?:角色|身份|转行|晋升|换工作|跳槽|成为|当上|升职|转岗|职业规划)",
            r"\b(?:career change|promotion|role change|become a|switch to|job change)\b",
        ],
        "repeated_stuck": [
            r"(?:又|再次|总是|反复|老是|一直|搞不定|解决不了|卡|绕回来|重蹈覆辙)",
            r"\b(?:again|once more|always|repeatedly|stuck again|same issue)\b",
        ],
    }

    def __init__(self, role_graph: Optional[RoleGraph] = None):
        self.role_graph = role_graph or RoleGraph()

    def route(
        self,
        query: str,
        recent_context: Optional[List[str]] = None,
    ) -> ReflectionRoute:
        """
        判断是否需要触发 Reflection

        Args:
            query: 用户当前输入
            recent_context: 近期用户消息摘要

        Returns:
            ReflectionRoute
        """
        # 1. 推断角色
        role_activation = self.role_graph.infer_role(query, recent_context)

        # 2. 分类场景
        scene, score = self._classify_scene(query, recent_context or [])

        # 3. 决定是否触发
        if scene == "default":
            return ReflectionRoute(
                should_reflect=False,
                scene="default",
                role=role_activation.role,
                role_activation=role_activation,
                reason=f"未识别到 Reflection 场景信号（role={role_activation.role.value}, confidence={role_activation.confidence}）",  # noqa: E501
            )

        # 4. 构造路由原因
        reason = f"识别到 {scene} 场景信号（score={score}），角色={role_activation.role.value}"

        return ReflectionRoute(
            should_reflect=True,
            scene=scene,
            role=role_activation.role,
            role_activation=role_activation,
            reason=reason,
        )

    def _classify_scene(self, query: str, recent_context: List[str]) -> tuple:
        """基于关键词匹配分类场景"""
        scores = {scene: 0 for scene in self.SCENES if scene != "default"}

        # query 权重高
        for scene, patterns in self.SCENE_SIGNALS.items():
            for pattern in patterns:
                matches = len(re.findall(pattern, query, re.IGNORECASE))
                scores[scene] += matches * 2

        # 近期上下文权重低
        for ctx in recent_context[-5:]:
            if not ctx:
                continue
            for scene, patterns in self.SCENE_SIGNALS.items():
                for pattern in patterns:
                    matches = len(re.findall(pattern, ctx, re.IGNORECASE))
                    scores[scene] += matches

        if not scores or max(scores.values()) <= 0:
            return "default", 0

        best_scene = max(scores, key=scores.get)  # type: ignore[arg-type]
        return best_scene, scores[best_scene]

    def get_role_graph(self) -> RoleGraph:
        """返回内部 RoleGraph（用于外部获取历史）"""
        return self.role_graph
