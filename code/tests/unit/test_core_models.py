import pytest
from pydantic import ValidationError

from core.models import DecisionKind, OperatorDecision


def test_approval_requires_an_explicit_candidate() -> None:
    with pytest.raises(ValidationError):
        OperatorDecision(
            engagement_id="engagement",
            decision=DecisionKind.APPROVE,
            state_version="state",
            matrix_version="matrix",
            operator="operator@example.test",
        )


def test_non_selection_decision_does_not_invent_a_candidate() -> None:
    decision = OperatorDecision(
        engagement_id="engagement",
        decision=DecisionKind.REQUEST_ALTERNATIVES,
        state_version="state",
        matrix_version="matrix",
        operator="operator@example.test",
    )
    assert decision.candidate_id is None

