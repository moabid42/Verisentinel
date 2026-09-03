from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from core.models import (
    ApprovalRecord,
    ExecutionRequest,
    ExecutionResult,
    ExecutionSpec,
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


class ExecutionAttemptStatus(StrEnum):
    """Lifecycle state of a one-time execution attempt."""

    RESERVED = "reserved"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExecutionAttempt(ImmutableModel):
    """Non-sensitive audit record that consumes one approval."""

    attempt_id: str
    approval_id: str
    engagement_id: str
    provider: Literal["simulator", "capsule", "evaluation", "gcp"]
    status: ExecutionAttemptStatus = ExecutionAttemptStatus.RESERVED
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    execution_id: str | None = None
    failure_code: str | None = None

    @model_validator(mode="after")
    def status_has_matching_outcome(self) -> ExecutionAttempt:
        """Require outcome fields to agree with the lifecycle state."""
        if self.status == ExecutionAttemptStatus.RESERVED:
            if self.completed_at or self.execution_id or self.failure_code:
                raise ValueError("reserved attempt cannot contain an outcome")
        elif self.status == ExecutionAttemptStatus.SUCCEEDED:
            if not self.completed_at or not self.execution_id or self.failure_code:
                raise ValueError("succeeded attempt requires one execution outcome")
        elif not self.completed_at or not self.failure_code or self.execution_id:
            raise ValueError("failed attempt requires one failure outcome")
        return self


class ExecutionRecord(ImmutableModel):
    request: ExecutionRequest
    approval: ApprovalRecord
    result: ExecutionResult
    spec: ExecutionSpec | None = None
    recorded_at: datetime = Field(default_factory=utc_now)
