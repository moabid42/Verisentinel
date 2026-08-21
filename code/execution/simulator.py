from __future__ import annotations

from core.ids import new_id
from core.models import ExecutionObservation, ExecutionRequest
from execution.models import SimulatorEffect
from execution.registry import ResolvedAction


class IAMSimulator:
    def __init__(self, effects: dict[str, SimulatorEffect] | None = None) -> None:
        self.effects = effects or {}

    def execute(
        self, request: ExecutionRequest, action: ResolvedAction
    ) -> ExecutionObservation:
        effect = self.effects.get(request.action_id, SimulatorEffect())
        footprint = tuple(
            action.matrix.permissions[index]
            for index in action.technique.footprint_indices
        )
        capabilities = tuple(
            sorted(set(action.technique.grants) | set(effect.gained_capabilities))
        )
        return ExecutionObservation(
            execution_id=new_id("execution"),
            engagement_id=request.engagement_id,
            action_id=request.action_id,
            identity=request.identity,
            target=request.target,
            success=effect.success,
            api_response_summary=effect.api_response_summary,
            gained_permissions=effect.gained_permissions,
            revoked_permissions=effect.revoked_permissions,
            gained_capabilities=capabilities,
            discovered_resources=effect.discovered_resources,
            observed_permission_footprint=footprint,
        )
