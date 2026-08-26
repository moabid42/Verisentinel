from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.config import Paths
from core.errors import DataConsistencyError, VersionConflictError
from core.models import (
    CreateEngagementRequest,
    DecisionKind,
    EngagementStatus,
    OperatorDecision,
    Proposal,
    ProposalBatch,
    ProposalRequest,
)
from environment.brain import EnvironmentBrain
from environment.repository import EnvironmentRepository
from execution.credentials import CredentialResolver, TokenMetadata, parse_credential_source
from execution.repository import ExecutionRepository
from execution.service import ExecutionService
from green_agent.orchestrator import GreenAgent
from green_agent.repository import GreenAgentRepository
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository
from launchpad.repository import LaunchpadRepository
from launchpad.service import LaunchpadService
from validator.service import ValidatorService

GREEN_ACCESS_TOKEN = "synthetic-green-agent-token"


class StaticProposer:
    def __init__(self, proposals: tuple[Proposal, ...]) -> None:
        self.proposals = proposals
        self.requests: list[ProposalRequest] = []

    def propose(self, request: ProposalRequest) -> ProposalBatch:
        self.requests.append(request)
        allowed = set(request.relevant_technique_ids)
        return ProposalBatch(
            proposals=tuple(
                proposal
                for proposal in self.proposals
                if not allowed or proposal.technique_id in allowed
            ),
            provider="fixture",
            model="fixture",
        )


class StaticTokenInspector:
    """Return an offline credential identity for orchestration tests."""

    def inspect(self, access_token: str) -> TokenMetadata:
        del access_token
        return TokenMetadata(
            principal="operator@example.test",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )


def services(tmp_path: Path, proposal_factory=None):
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    uncovered = tuple(
        technique
        for technique in matrix.techniques.values()
        if not set(technique.footprint_indices).intersection(matrix.coverage_indices)
    )
    assert uncovered
    selected = uncovered[0]
    proposal = Proposal(
        candidate_id="candidate",
        technique_id=selected.technique_id,
        action_id=f"technique:{selected.technique_id}",
        identity="operator@example.test",
        target="projects/sandbox",
        rationale="fixture",
        rank=1,
    )
    proposer = (
        proposal_factory(matrix, uncovered, proposal)
        if proposal_factory
        else StaticProposer((proposal,))
    )
    environment = EnvironmentBrain(
        repository=EnvironmentRepository(tmp_path / "environment"), snapshots=snapshots
    )
    launchpad = LaunchpadService(
        repository=LaunchpadRepository(tmp_path / "launchpad")
    )
    credential_resolver = CredentialResolver(
        environ={"EXECUTION_TOKEN": GREEN_ACCESS_TOKEN},
        token_inspector=StaticTokenInspector(),
    )
    credential_resolver.register(
        "credential-a",
        parse_credential_source("env:EXECUTION_TOKEN"),
    )
    execution = ExecutionService(
        repository=ExecutionRepository(tmp_path / "execution"),
        snapshots=snapshots,
        credential_resolver=credential_resolver,
        enabled=True,
    )
    green = GreenAgent(
        repository=GreenAgentRepository(tmp_path / "green"),
        snapshots=snapshots,
        environment=environment,
        proposer=proposer,
        validator=ValidatorService(snapshots=snapshots),
        launchpad=launchpad,
        execution=execution,
    )
    permissions = tuple(matrix.permissions[index] for index in selected.required_indices)
    engagement = green.create(
        CreateEngagementRequest(
            objective=selected.title,
            identity="operator@example.test",
            credential="credential-a",
            target_scope="projects/sandbox",
            permissions=permissions,
        )
    )
    return green, launchpad, execution, engagement, proposal


def decision(engagement, kind: DecisionKind, candidate_id: str | None = None):
    return OperatorDecision(
        engagement_id=engagement.engagement_id,
        decision=kind,
        candidate_id=candidate_id,
        state_version=engagement.state_version,
        matrix_version=engagement.matrix_version,
        operator="human@example.test",
    )


def test_green_agent_publishes_only_admissible_candidates(tmp_path: Path) -> None:
    green, launchpad, execution, engagement, proposal = services(tmp_path)

    result = green.cycle(engagement.engagement_id)

    assert result.status == EngagementStatus.AWAITING_APPROVAL
    assert [card.proposal.candidate_id for card in result.candidates] == [
        proposal.candidate_id
    ]
    assert launchpad.candidates(engagement.engagement_id).candidates == result.candidates
    assert execution.repository.executions.list_keys() == ()


def test_proposal_model_input_excludes_credential_values(tmp_path: Path) -> None:
    green, _, _, engagement, _ = services(tmp_path)

    green.cycle(engagement.engagement_id)

    proposer = green.proposer
    assert isinstance(proposer, StaticProposer)
    captured = proposer.requests[0].model_dump_json()
    assert GREEN_ACCESS_TOKEN not in captured
    assert '"credential-a"' not in captured


def test_explicit_approval_executes_and_versions_the_environment(tmp_path: Path) -> None:
    green, _, execution, engagement, proposal = services(tmp_path)
    cycle = green.cycle(engagement.engagement_id)
    old_state = engagement.state_version

    next_cycle = green.decide(
        decision(
            green.get(engagement.engagement_id),
            DecisionKind.APPROVE,
            proposal.candidate_id,
        )
    )

    assert execution.repository.executions.list_keys()
    assert green.environment.current(engagement.engagement_id).state_version != old_state
    assert next_cycle.status == EngagementStatus.AWAITING_APPROVAL
    assert not next_cycle.candidates
    assert cycle.candidates[0].proposal.action_id in green.environment.current(
        engagement.engagement_id
    ).completed_actions


def test_stale_operator_decision_executes_nothing(tmp_path: Path) -> None:
    green, _, execution, engagement, proposal = services(tmp_path)
    green.cycle(engagement.engagement_id)
    stale = decision(
        green.get(engagement.engagement_id),
        DecisionKind.APPROVE,
        proposal.candidate_id,
    ).model_copy(update={"state_version": "sha256:stale"})

    with pytest.raises(VersionConflictError, match="stale environment state"):
        green.decide(stale)
    assert execution.repository.executions.list_keys() == ()


def test_unknown_proposal_never_reaches_launchpad(tmp_path: Path) -> None:
    def unknown_factory(matrix, uncovered, proposal):
        return StaticProposer(
            (
                proposal.model_copy(
                    update={
                        "candidate_id": "unknown",
                        "technique_id": "not-in-catalog",
                        "action_id": "technique:not-in-catalog",
                    }
                ),
            )
        )

    green, launchpad, _, engagement, _ = services(tmp_path, unknown_factory)

    result = green.cycle(engagement.engagement_id)

    assert not result.candidates
    assert not launchpad.candidates(engagement.engagement_id).candidates


def test_green_agent_never_publishes_more_than_three_candidates(tmp_path: Path) -> None:
    def many_factory(matrix, uncovered, proposal):
        choices = uncovered[:5]
        return StaticProposer(
            tuple(
                Proposal(
                    candidate_id=f"candidate-{index}",
                    technique_id=technique.technique_id,
                    action_id=f"technique:{technique.technique_id}",
                    identity=proposal.identity,
                    target=proposal.target,
                    rationale="fixture",
                    rank=index,
                )
                for index, technique in enumerate(choices, start=1)
            )
        )

    green, _, _, engagement, _ = services(tmp_path, many_factory)
    matrix = green.snapshots.get(engagement.matrix_version)
    proposer = green.proposer
    required = {
        matrix.permissions[index]
        for proposal in proposer.proposals
        for index in matrix.techniques[proposal.technique_id].required_indices
    }
    current = green.environment.current(engagement.engagement_id)
    vector = green.environment.state_vector(
        engagement.engagement_id, engagement.identity, engagement.target_scope
    )
    indices = tuple(sorted(matrix.permissions.index(permission) for permission in required))
    updated_vector = vector.model_copy(update={"permission_indices": indices})
    updated_environment = current.model_copy(
        update={"identities": {next(iter(current.identities)): updated_vector}}
    )
    green.environment.repository.publish(updated_environment)

    result = green.cycle(engagement.engagement_id)

    assert len(result.candidates) == 3


def test_terminated_engagement_cannot_execute(tmp_path: Path) -> None:
    green, _, execution, engagement, proposal = services(tmp_path)
    green.cycle(engagement.engagement_id)
    current = green.get(engagement.engagement_id)

    result = green.decide(decision(current, DecisionKind.TERMINATE))

    assert result.status == EngagementStatus.TERMINATED
    assert not execution.repository.authorization(engagement.engagement_id).enabled
    with pytest.raises(DataConsistencyError, match="terminal"):
        green.cycle(engagement.engagement_id)
