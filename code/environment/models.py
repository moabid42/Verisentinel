from __future__ import annotations

from core.models import ExecutionObservation, ImmutableModel


class InitializeEnvironmentRequest(ImmutableModel):
    engagement_id: str
    objective: str
    matrix_version: str
    identity: str
    credential: str
    scope: str
    permissions: tuple[str, ...]
    source: str


class ApplyObservationRequest(ImmutableModel):
    expected_state_version: str
    observation: ExecutionObservation

