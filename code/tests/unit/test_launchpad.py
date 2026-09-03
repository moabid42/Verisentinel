from pathlib import Path

import pytest

from core.errors import DataConsistencyError
from core.models import (
    ActionCommand,
    CandidateCard,
    CandidateValidationResult,
    DecisionKind,
    OperatorDecision,
    Proposal,
    ReviewStage,
)
from launchpad.models import CandidateSet
from launchpad.repository import LaunchpadRepository
from launchpad.service import LaunchpadService


def card(candidate_id: str, rank: int = 1) -> CandidateCard:
    proposal = Proposal(
        candidate_id=candidate_id,
        technique_id="technique",
        action_id="technique:technique",
        identity="identity",
        target="projects/sandbox",
        rationale="fixture",
        rank=rank,
    )
    validation = CandidateValidationResult(
        result_id=f"validation-{candidate_id}",
        candidate_id=candidate_id,
        technique_id="technique",
        admissible=True,
        feasible=True,
        outside_loaded_coverage=True,
        missing_permissions=(),
        covered_permissions=(),
        matching_detection_ids=(),
        state_version="state",
        matrix_version="matrix",
        explanation="admissible",
    )
    return CandidateCard(
        proposal=proposal, validation=validation, required_permissions=("test.permission",)
    )


def candidate_set(*cards: CandidateCard) -> CandidateSet:
    return CandidateSet(
        engagement_id="engagement",
        objective="test",
        state_version="state",
        matrix_version="matrix",
        candidates=cards,
    )


def test_launchpad_persists_review_stage(tmp_path: Path) -> None:
    service = LaunchpadService(repository=LaunchpadRepository(tmp_path))
    action_card = card("candidate-1").model_copy(
        update={
            "action_command": ActionCommand(
                action_id="technique:technique",
                approval_id="approval_" + "1" * 32,
                display="catalog.technique technique:technique",
            )
        }
    )

    published = service.publish(
        candidate_set(action_card).model_copy(
            update={"review_stage": ReviewStage.ACTION_EXECUTION}
        )
    )

    assert published.review_stage == ReviewStage.ACTION_EXECUTION
    assert service.candidates("engagement").review_stage == ReviewStage.ACTION_EXECUTION


def test_launchpad_persists_typed_action_command_preview(tmp_path: Path) -> None:
    service = LaunchpadService(repository=LaunchpadRepository(tmp_path))
    action_card = card("candidate-1").model_copy(
        update={
            "action_command": ActionCommand(
                action_id="technique:technique",
                approval_id="approval_" + "1" * 32,
                display="/usr/local/bin/python /workspace/action.py",
            )
        }
    )

    published = service.publish(candidate_set(action_card))

    assert published.candidates[0].action_command == action_card.action_command


def test_launchpad_rejects_action_review_without_command_preview(
    tmp_path: Path,
) -> None:
    service = LaunchpadService(repository=LaunchpadRepository(tmp_path))
    action_set = candidate_set(card("candidate-1")).model_copy(
        update={"review_stage": ReviewStage.ACTION_EXECUTION}
    )

    with pytest.raises(DataConsistencyError, match="command preview"):
        service.publish(action_set)


def test_launchpad_rejects_more_than_three_candidates(tmp_path: Path) -> None:
    service = LaunchpadService(repository=LaunchpadRepository(tmp_path))
    with pytest.raises(DataConsistencyError):
        service.publish(candidate_set(*(card(f"candidate-{n}", n) for n in range(1, 5))))


def test_launchpad_never_defaults_to_an_unselected_candidate(tmp_path: Path) -> None:
    service = LaunchpadService(repository=LaunchpadRepository(tmp_path))
    service.publish(candidate_set(card("candidate-1")))
    receipt = service.decide(
        OperatorDecision(
            engagement_id="engagement",
            decision=DecisionKind.REQUEST_ALTERNATIVES,
            state_version="state",
            matrix_version="matrix",
            operator="operator@example.test",
        )
    )
    assert receipt.decision.candidate_id is None
    assert not receipt.forwarded


def test_launchpad_approval_must_select_a_displayed_candidate(tmp_path: Path) -> None:
    service = LaunchpadService(repository=LaunchpadRepository(tmp_path))
    service.publish(candidate_set(card("candidate-1")))
    with pytest.raises(DataConsistencyError):
        service.decide(
            OperatorDecision(
                engagement_id="engagement",
                decision=DecisionKind.APPROVE,
                candidate_id="not-displayed",
                state_version="state",
                matrix_version="matrix",
                operator="operator@example.test",
            )
        )
