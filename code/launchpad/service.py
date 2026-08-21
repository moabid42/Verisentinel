from __future__ import annotations

from core.config import Paths
from core.errors import DataConsistencyError, VersionConflictError
from core.ids import new_id
from core.models import DecisionKind, OperatorDecision, utc_now
from launchpad.models import CandidateSet, DecisionReceipt
from launchpad.repository import LaunchpadRepository
from launchpad.sink import DecisionSink


class LaunchpadService:
    def __init__(
        self,
        repository: LaunchpadRepository | None = None,
        sink: DecisionSink | None = None,
        paths: Paths | None = None,
    ) -> None:
        paths = paths or Paths()
        self.repository = repository or LaunchpadRepository(paths.runtime / "launchpad")
        self.sink = sink

    def publish(self, candidate_set: CandidateSet) -> CandidateSet:
        if len(candidate_set.candidates) > 3:
            raise DataConsistencyError("the launchpad accepts at most three candidates")
        if any(not card.validation.admissible for card in candidate_set.candidates):
            raise DataConsistencyError("rejected candidates cannot be published to the launchpad")
        if any(
            card.validation.state_version != candidate_set.state_version
            or card.validation.matrix_version != candidate_set.matrix_version
            for card in candidate_set.candidates
        ):
            raise VersionConflictError(
                "candidate validation versions do not match the candidate set"
            )
        if candidate_set.state_analysis is not None and (
            candidate_set.state_analysis.state_version != candidate_set.state_version
            or candidate_set.state_analysis.matrix_version != candidate_set.matrix_version
        ):
            raise VersionConflictError(
                "state analysis versions do not match the candidate set"
            )
        identifiers = [card.proposal.candidate_id for card in candidate_set.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise DataConsistencyError("candidate identifiers must be unique")
        return self.repository.publish_candidates(candidate_set)

    def candidates(self, engagement_id: str) -> CandidateSet:
        return self.repository.get_candidates(engagement_id)

    def decide(self, decision: OperatorDecision) -> DecisionReceipt:
        candidates = self.candidates(decision.engagement_id)
        if decision.state_version != candidates.state_version:
            raise VersionConflictError("operator decision uses a stale environment state")
        if decision.matrix_version != candidates.matrix_version:
            raise VersionConflictError("operator decision uses a stale matrix snapshot")
        known = {card.proposal.candidate_id for card in candidates.candidates}
        if decision.candidate_id is not None and decision.candidate_id not in known:
            raise DataConsistencyError(
                f"candidate {decision.candidate_id!r} is not displayed for this engagement"
            )
        if decision.decision == DecisionKind.APPROVE and decision.candidate_id is None:
            raise DataConsistencyError("approval requires an explicit displayed candidate")

        decision_id = new_id("decision")
        if self.sink is not None:
            self.sink.send(decision)
        return self.repository.record_decision(
            DecisionReceipt(
                decision_id=decision_id,
                decision=decision,
                forwarded=self.sink is not None,
                recorded_at=utc_now(),
            )
        )
