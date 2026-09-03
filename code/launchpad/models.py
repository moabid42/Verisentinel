from __future__ import annotations

from datetime import datetime

from core.models import (
    CandidateCard,
    ImmutableModel,
    OperatorDecision,
    ReviewStage,
    StateValidationResult,
)


class CandidateSet(ImmutableModel):
    engagement_id: str
    objective: str
    state_version: str
    matrix_version: str
    candidates: tuple[CandidateCard, ...]
    review_stage: ReviewStage = ReviewStage.TECHNIQUE_SELECTION
    identity: str = ""
    scope: str = ""
    state_analysis: StateValidationResult | None = None


class DecisionReceipt(ImmutableModel):
    decision_id: str
    decision: OperatorDecision
    forwarded: bool
    recorded_at: datetime
