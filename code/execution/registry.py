from __future__ import annotations

from dataclasses import dataclass

from core.errors import AuthorizationError, NotFoundError
from core.models import ExecutionRequest, MatrixSnapshot, TechniqueDefinition
from ingestion.snapshot import SnapshotRepository


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    matrix: MatrixSnapshot
    technique: TechniqueDefinition


class ActionRegistry:
    """Resolves the finite technique catalog into executable typed actions."""

    def __init__(self, snapshots: SnapshotRepository) -> None:
        self.snapshots = snapshots

    def resolve(self, request: ExecutionRequest) -> ResolvedAction:
        if request.arguments:
            raise AuthorizationError(
                "technique actions do not accept arbitrary execution arguments"
            )
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
        return ResolvedAction(matrix=matrix, technique=technique)
