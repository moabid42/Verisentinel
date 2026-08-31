from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CREDENTIAL_REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImmutableModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IngestionIssue(ImmutableModel):
    severity: Literal["warning", "error"]
    code: str
    source_id: str
    permission: str | None = None
    message: str


class IngestionReport(ImmutableModel):
    report_id: str
    created_at: datetime = Field(default_factory=utc_now)
    permission_count: int
    detection_count: int
    technique_count: int
    enabled_detection_count: int
    issues: tuple[IngestionIssue, ...] = ()

    @property
    def errors(self) -> tuple[IngestionIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")


class DetectionDefinition(ImmutableModel):
    detection_id: str
    title: str
    source: str
    paradigm: str
    permission_indices: tuple[int, ...]


class TechniqueDefinition(ImmutableModel):
    technique_id: str
    title: str
    tactic: str
    service: str
    required_indices: tuple[int, ...]
    footprint_indices: tuple[int, ...]
    optional_indices: tuple[int, ...] = ()
    footprint_assumption: Literal["required_permissions", "observed_footprint"] = (
        "required_permissions"
    )
    grants: tuple[str, ...] = ()


class MatrixSnapshot(ImmutableModel):
    matrix_version: str
    iam_dataset_version: str
    iamouflage_version: str
    created_at: datetime = Field(default_factory=utc_now)
    permissions: tuple[str, ...]
    enabled_detection_ids: tuple[str, ...]
    detections: dict[str, DetectionDefinition]
    coverage_indices: tuple[int, ...]
    techniques: dict[str, TechniqueDefinition]
    report: IngestionReport

    @model_validator(mode="after")
    def validate_indices(self) -> MatrixSnapshot:
        width = len(self.permissions)
        index_sets = [self.coverage_indices]
        index_sets.extend(d.permission_indices for d in self.detections.values())
        for technique in self.techniques.values():
            index_sets.extend(
                (
                    technique.required_indices,
                    technique.footprint_indices,
                    technique.optional_indices,
                )
            )
        if any(index < 0 or index >= width for values in index_sets for index in values):
            raise ValueError("snapshot contains a permission index outside its column universe")
        if tuple(sorted(self.permissions)) != self.permissions:
            raise ValueError("permission columns must be sorted deterministically")
        if set(self.enabled_detection_ids) != set(self.detections):
            raise ValueError("enabled detection IDs and detection rows differ")
        return self


class StateVector(ImmutableModel):
    state_version: str
    engagement_id: str
    matrix_version: str
    identity: str
    credential: str
    scope: str
    permission_indices: tuple[int, ...]
    source: str
    created_at: datetime = Field(default_factory=utc_now)


class EnvironmentSnapshot(ImmutableModel):
    engagement_id: str
    state_version: str
    matrix_version: str
    objective: str
    identities: dict[str, StateVector]
    discovered_resources: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    completed_actions: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class ExecutionObservation(ImmutableModel):
    execution_id: str
    engagement_id: str
    action_id: str
    identity: str
    target: str
    success: bool
    api_response_summary: str = Field(max_length=4096)
    command_stdout: str = Field(default="", max_length=32_768)
    explanation: str = Field(default="", max_length=4096)
    next_steps: tuple[str, ...] = ()
    gained_permissions: tuple[str, ...] = ()
    revoked_permissions: tuple[str, ...] = ()
    gained_capabilities: tuple[str, ...] = ()
    discovered_resources: tuple[str, ...] = ()
    observed_permission_footprint: tuple[str, ...] = ()
    timestamp: datetime = Field(default_factory=utc_now)


class StateValidationRequest(ImmutableModel):
    state: StateVector
    matrix_version: str | None = None


class StateValidationResult(ImmutableModel):
    result_id: str
    state_version: str
    matrix_version: str
    monitored_permission_indices: tuple[int, ...]
    unmonitored_permission_indices: tuple[int, ...]
    monitored_permissions: tuple[str, ...]
    unmonitored_permissions: tuple[str, ...]
    has_gap: bool


class CandidateValidationRequest(ImmutableModel):
    candidate_id: str
    technique_id: str
    state: StateVector
    matrix_version: str | None = None


class CandidateValidationResult(ImmutableModel):
    result_id: str
    candidate_id: str
    technique_id: str
    admissible: bool
    feasible: bool
    outside_loaded_coverage: bool
    missing_permissions: tuple[str, ...]
    covered_permissions: tuple[str, ...]
    matching_detection_ids: tuple[str, ...]
    state_version: str
    matrix_version: str
    explanation: str
    uncovered_permissions: tuple[str, ...] = ()


class ProposalRequest(ImmutableModel):
    engagement_id: str
    objective: str
    state_version: str
    matrix_version: str
    identity: str
    environment_summary: dict[str, Any]
    relevant_technique_ids: tuple[str, ...] = ()
    # Techniques already validated and rejected in earlier rounds of this step. The agentic
    # proposer is told these explicitly so it does not re-propose an ID it already burned.
    excluded_technique_ids: tuple[str, ...] = ()
    previous_rejections: tuple[str, ...] = ()
    maximum_proposals: int = Field(default=10, ge=1, le=50)


class Proposal(ImmutableModel):
    candidate_id: str
    technique_id: str
    action_id: str
    identity: str
    target: str
    rationale: str
    rank: int = Field(ge=1)


class ProposalBatch(ImmutableModel):
    proposals: tuple[Proposal, ...]
    provider: str
    model: str


class ActionArtifact(ImmutableModel):
    """One complete model-authored file proposed for controlled execution."""

    path: Literal["action.py"] = "action.py"
    content: str = Field(min_length=1, max_length=65_536)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_model: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=2048)
    written: bool = False

    @model_validator(mode="after")
    def digest_matches_content(self) -> ActionArtifact:
        expected = "sha256:" + hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.digest != expected:
            raise ValueError("artifact digest does not match its content")
        return self


class ActionAuthorRequest(ImmutableModel):
    """Bounded context from which a model may author one action artifact."""

    engagement_id: str
    approval_id: str = Field(pattern=r"^approval_[0-9a-f]{32}$")
    action_id: str = Field(pattern=r"^technique:\S+$")
    technique_id: str
    technique_title: str
    objective: str
    identity: str
    target: str
    rationale: str
    required_permissions: tuple[str, ...]
    observed_permissions: tuple[str, ...]
    expected_capabilities: tuple[str, ...]


class ActionCommand(ImmutableModel):
    """Exact command preview for one registered typed action."""

    action_id: str = Field(pattern=r"^technique:\S+$")
    approval_id: str | None = Field(
        default=None,
        pattern=r"^approval_[0-9a-f]{32}$",
    )
    display: str = Field(min_length=1, max_length=4096)
    provider_operation: Literal["catalog.technique"] = "catalog.technique"
    parameter_model: Literal["technique.none.v1"] = "technique.none.v1"
    prepared_by: Literal["deterministic_action_resolver", "model"] = (
        "deterministic_action_resolver"
    )
    input_summary: str = Field(
        default="Typed action input is assembled in memory after approval.",
        min_length=1,
        max_length=1024,
    )
    input_preview: str = Field(default="{}", min_length=2, max_length=65_536)
    tool_source: str = Field(default="", max_length=256)
    tool_installation: str = Field(default="", max_length=512)
    side_effects: tuple[str, ...] = ()
    artifact: ActionArtifact | None = None


class CandidateCard(ImmutableModel):
    proposal: Proposal
    validation: CandidateValidationResult
    required_permissions: tuple[str, ...]
    technique_title: str = ""
    expected_capabilities: tuple[str, ...] = ()
    action_command: ActionCommand | None = None


class DecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REJECT_ALL = "reject_all"
    REQUEST_ALTERNATIVES = "request_alternatives"
    TERMINATE = "terminate"


class ReviewStage(StrEnum):
    """Operator review stage for an active engagement."""

    TECHNIQUE_SELECTION = "technique_selection"
    ACTION_EXECUTION = "action_execution"


class OperatorDecision(ImmutableModel):
    engagement_id: str
    decision: DecisionKind
    candidate_id: str | None = None
    state_version: str
    matrix_version: str
    operator: str
    reason: str = ""

    @model_validator(mode="after")
    def approved_candidate_is_explicit(self) -> OperatorDecision:
        if self.decision in {DecisionKind.APPROVE, DecisionKind.REJECT} and not self.candidate_id:
            raise ValueError(f"{self.decision} requires an explicit candidate_id")
        return self


class ApprovalRecord(ImmutableModel):
    approval_id: str
    engagement_id: str
    candidate_id: str
    action_id: str
    identity: str
    target: str
    arguments_digest: str
    validator_result_id: str
    state_version: str
    matrix_version: str
    operator: str
    credential_ref: str = Field(
        default="",
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._/-]{0,127})?$",
    )
    approved_at: datetime = Field(default_factory=utc_now)


class TechniqueActionParameters(ImmutableModel):
    """The strict parameter model for current catalog technique actions."""


class ActionDefinition(ImmutableModel):
    """A registered provider-neutral action definition."""

    action_id: str = Field(pattern=r"^technique:\S+$")
    provider_operation: Literal["catalog.technique"] = "catalog.technique"
    parameter_model: Literal["technique.none.v1"] = "technique.none.v1"
    technique_id: str = Field(min_length=1)
    observed_permission_footprint: tuple[str, ...] = ()
    expected_capabilities: tuple[str, ...] = ()


class ExecutionRequest(ImmutableModel):
    engagement_id: str
    candidate_id: str
    action_id: str
    identity: str
    target: str
    arguments: TechniqueActionParameters = Field(
        default_factory=TechniqueActionParameters
    )
    validator_result_id: str
    approval_id: str
    state_version: str
    matrix_version: str
    credential_ref: str = Field(
        default="",
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._/-]{0,127})?$",
    )


class ExecutionSpec(ImmutableModel):
    """Immutable provider input derived from an approved execution request."""

    engagement_id: str
    candidate_id: str
    action: ActionDefinition
    identity: str
    target: str
    arguments: TechniqueActionParameters
    validator_result_id: str
    approval_id: str
    state_version: str
    matrix_version: str
    credential_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=CREDENTIAL_REFERENCE_PATTERN,
    )
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    output_limit_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)


class ExecutionResult(ImmutableModel):
    observation: ExecutionObservation
    provider: Literal["simulator", "capsule", "evaluation", "gcp"]


class EngagementStatus(StrEnum):
    CREATED = "created"
    PROPOSING = "proposing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    TERMINATED = "terminated"
    FAILED = "failed"


class Engagement(StrictModel):
    engagement_id: str
    objective: str
    identity: str
    target_scope: str
    status: EngagementStatus = EngagementStatus.CREATED
    state_version: str
    matrix_version: str
    proposal_round: int = 0
    candidates: dict[str, CandidateCard] = Field(default_factory=dict)
    excluded_technique_ids: tuple[str, ...] = ()
    rejection_feedback: tuple[str, ...] = ()
    pending_approval_id: str | None = None
    review_stage: ReviewStage = ReviewStage.TECHNIQUE_SELECTION
    selected_technique_id: str | None = None
    last_error: str | None = None


class CreateEngagementRequest(ImmutableModel):
    objective: str
    identity: str
    credential: str
    target_scope: str
    permissions: tuple[str, ...]
    state_source: str = "operator"


class CycleResult(ImmutableModel):
    engagement_id: str
    status: EngagementStatus
    proposal_round: int
    review_stage: ReviewStage = ReviewStage.TECHNIQUE_SELECTION
    candidates: tuple[CandidateCard, ...] = ()
    executed_command: ActionCommand | None = None
    execution_observation: ExecutionObservation | None = None
    resulting_state_version: str | None = None
    message: str
