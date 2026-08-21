from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from core.models import (
    ApprovalRecord,
    ExecutionRequest,
    ExecutionResult,
    ImmutableModel,
    utc_now,
)


class EngagementAuthorization(ImmutableModel):
    engagement_id: str
    allowed_targets: tuple[str, ...]
    state_version: str
    matrix_version: str
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def targets_are_explicit(self) -> EngagementAuthorization:
        if not self.allowed_targets or any(not target.strip() for target in self.allowed_targets):
            raise ValueError("at least one non-empty target scope is required")
        return self


class SimulatorEffect(ImmutableModel):
    success: bool = True
    api_response_summary: str = "simulated registered IAM action"
    gained_permissions: tuple[str, ...] = ()
    revoked_permissions: tuple[str, ...] = ()
    gained_capabilities: tuple[str, ...] = ()
    discovered_resources: tuple[str, ...] = ()


class ExecutionRecord(ImmutableModel):
    request: ExecutionRequest
    approval: ApprovalRecord
    result: ExecutionResult
    recorded_at: datetime = Field(default_factory=utc_now)
