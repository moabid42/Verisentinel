from __future__ import annotations

from typing import Literal

from core.ids import new_id
from core.models import ExecutionObservation, ExecutionSpec
from execution.credentials import CredentialLease
from execution.models import SimulatorEffect


class SimulatorExecutionProvider:
    """Deterministically simulate registered technique actions."""

    name: Literal["simulator"] = "simulator"

    def __init__(self, effects: dict[str, SimulatorEffect] | None = None) -> None:
        self.effects = effects or {}

    def execute(
        self,
        spec: ExecutionSpec,
        credential_lease: CredentialLease,
    ) -> ExecutionObservation:
        del credential_lease
        effect = self.effects.get(spec.action.action_id, SimulatorEffect())
        capabilities = tuple(
            sorted(
                set(spec.action.expected_capabilities)
                | set(effect.gained_capabilities)
            )
        )
        return ExecutionObservation(
            execution_id=new_id("execution"),
            engagement_id=spec.engagement_id,
            action_id=spec.action.action_id,
            identity=spec.identity,
            target=spec.target,
            success=effect.success,
            api_response_summary=effect.api_response_summary,
            gained_permissions=effect.gained_permissions,
            revoked_permissions=effect.revoked_permissions,
            gained_capabilities=capabilities,
            discovered_resources=effect.discovered_resources,
            observed_permission_footprint=spec.action.observed_permission_footprint,
        )
