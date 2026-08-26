from __future__ import annotations

from dataclasses import dataclass

from core.errors import AuthorizationError, NotFoundError
from core.models import (
    ActionDefinition,
    ExecutionRequest,
    ExecutionSpec,
    MatrixSnapshot,
    TechniqueDefinition,
)
from ingestion.snapshot import SnapshotRepository


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    matrix: MatrixSnapshot
    technique: TechniqueDefinition
    definition: ActionDefinition
    spec: ExecutionSpec


class ActionRegistry:
    """Resolves the finite technique catalog into executable typed actions."""

    def __init__(self, snapshots: SnapshotRepository) -> None:
        self.snapshots = snapshots

    def resolve(self, request: ExecutionRequest) -> ResolvedAction:
        if not request.credential_ref:
            raise AuthorizationError("execution request has no credential reference")
        try:
            matrix = self.snapshots.get(request.matrix_version)
        except NotFoundError as error:
            raise AuthorizationError("execution references an unknown matrix snapshot") from error
        prefix = "technique:"
        if not request.action_id.startswith(prefix):
            raise AuthorizationError(f"action {request.action_id!r} is not registered")
        technique_id = request.action_id.removeprefix(prefix)
        technique = matrix.techniques.get(technique_id)
        if technique is None or request.action_id != f"technique:{technique.technique_id}":
            raise AuthorizationError(f"action {request.action_id!r} is not registered")
        definition = ActionDefinition(
            action_id=request.action_id,
            technique_id=technique.technique_id,
            observed_permission_footprint=tuple(
                matrix.permissions[index] for index in technique.footprint_indices
            ),
            expected_capabilities=technique.grants,
        )
        spec = ExecutionSpec(
            engagement_id=request.engagement_id,
            candidate_id=request.candidate_id,
            action=definition,
            identity=request.identity,
            target=request.target,
            arguments=request.arguments,
            validator_result_id=request.validator_result_id,
            approval_id=request.approval_id,
            state_version=request.state_version,
            matrix_version=request.matrix_version,
            credential_ref=request.credential_ref,
        )
        return ResolvedAction(
            matrix=matrix,
            technique=technique,
            definition=definition,
            spec=spec,
        )
