from __future__ import annotations

from threading import RLock

from core.config import Paths
from core.errors import DataConsistencyError, VersionConflictError
from core.ids import new_id
from core.models import (
    ApprovalRecord,
    CandidateCard,
    CandidateValidationRequest,
    CreateEngagementRequest,
    CycleResult,
    DecisionKind,
    Engagement,
    EngagementStatus,
    ExecutionRequest,
    OperatorDecision,
    Proposal,
    ProposalRequest,
    StateValidationRequest,
)
from core.security import stable_digest
from core.tracing import DebugTrace
from environment.brain import EnvironmentBrain
from environment.models import ApplyObservationRequest, InitializeEnvironmentRequest
from execution.guardrails import target_is_allowed
from execution.models import EngagementAuthorization
from execution.service import ExecutionService
from green_agent.gateways import CandidatePublisher, ExecutionGateway
from green_agent.repository import GreenAgentRepository
from ingestion.snapshot import SnapshotRepository
from launchpad.models import CandidateSet
from launchpad.service import LaunchpadService
from proposer.service import ProposerService
from validator.service import ValidatorService


class GreenAgent:
    def __init__(
        self,
        repository: GreenAgentRepository | None = None,
        snapshots: SnapshotRepository | None = None,
        environment: EnvironmentBrain | None = None,
        proposer: ProposerService | None = None,
        validator: ValidatorService | None = None,
        launchpad: CandidatePublisher | None = None,
        execution: ExecutionGateway | None = None,
        paths: Paths | None = None,
        maximum_rounds_per_cycle: int = 3,
        proposals_per_round: int = 10,
        auto_cycle_after_execution: bool = True,
        trace: DebugTrace | None = None,
    ) -> None:
        paths = paths or Paths()
        self.repository = repository or GreenAgentRepository(paths.runtime / "green-agent")
        self.snapshots = snapshots or SnapshotRepository(paths.artifacts / "snapshots")
        self.environment = environment or EnvironmentBrain(snapshots=self.snapshots, paths=paths)
        self.proposer = proposer or ProposerService(snapshots=self.snapshots, paths=paths)
        self.validator = validator or ValidatorService(snapshots=self.snapshots, paths=paths)
        self.launchpad = launchpad or LaunchpadService(paths=paths)
        self.execution = execution or ExecutionService(snapshots=self.snapshots, paths=paths)
        self.maximum_rounds_per_cycle = maximum_rounds_per_cycle
        self.proposals_per_round = proposals_per_round
        self.auto_cycle_after_execution = auto_cycle_after_execution
        self.trace = trace
        self._lock = RLock()

    def create(self, request: CreateEngagementRequest) -> Engagement:
        with self._lock:
            matrix = self.snapshots.current()
            engagement_id = new_id("engagement")
            environment = self.environment.initialize(
                InitializeEnvironmentRequest(
                    engagement_id=engagement_id,
                    objective=request.objective,
                    matrix_version=matrix.matrix_version,
                    identity=request.identity,
                    credential=request.credential,
                    scope=request.target_scope,
                    permissions=request.permissions,
                    source=request.state_source,
                )
            )
            engagement = Engagement(
                engagement_id=engagement_id,
                objective=request.objective,
                identity=request.identity,
                target_scope=request.target_scope,
                state_version=environment.state_version,
                matrix_version=matrix.matrix_version,
            )
            self.execution.authorize(self._authorization(engagement, enabled=True))
            saved = self.repository.save(engagement)
            self._trace(
                "engagement_created",
                engagement_id=engagement_id,
                identity=request.identity,
                target_scope=request.target_scope,
                matrix_version=matrix.matrix_version,
                state_version=environment.state_version,
                permission_count=len(request.permissions),
            )
            return saved

    def get(self, engagement_id: str) -> Engagement:
        return self.repository.get(engagement_id)

    def cycle(self, engagement_id: str) -> CycleResult:
        with self._lock:
            engagement = self.get(engagement_id)
            self._require_active(engagement)
            self._trace(
                "cycle_started",
                engagement_id=engagement_id,
                proposal_round=engagement.proposal_round,
                state_version=engagement.state_version,
                matrix_version=engagement.matrix_version,
            )
            environment = self.environment.current(engagement_id)
            if environment.matrix_version != engagement.matrix_version:
                raise VersionConflictError("environment and engagement matrix versions differ")
            state = self.environment.state_vector(
                engagement_id, engagement.identity, engagement.target_scope
            )
            matrix = self.snapshots.get(engagement.matrix_version)
            state_result = self.validator.validate_state(StateValidationRequest(state=state))
            self._trace(
                "state_validated",
                engagement_id=engagement_id,
                available_permission_count=len(state.permission_indices),
                monitored_permission_count=len(state_result.monitored_permission_indices),
                unmonitored_permission_count=len(state_result.unmonitored_permission_indices),
                has_gap=state_result.has_gap,
            )

            engagement.status = EngagementStatus.PROPOSING
            engagement.state_version = environment.state_version
            engagement.candidates = {}
            engagement.last_error = None
            self.repository.save(engagement)

            accepted: list[CandidateCard] = []
            excluded = set(engagement.excluded_technique_ids)
            excluded.update(
                action.removeprefix("technique:")
                for action in environment.completed_actions
                if action.startswith("technique:")
            )
            feedback = list(engagement.rejection_feedback)
            seen_candidates: set[str] = set()

            for cycle_round in range(1, self.maximum_rounds_per_cycle + 1):
                if len(accepted) >= 3:
                    break
                remaining = tuple(sorted(set(matrix.techniques) - excluded))
                if not remaining:
                    break
                proposal_request = ProposalRequest(
                    engagement_id=engagement.engagement_id,
                    objective=engagement.objective,
                    state_version=environment.state_version,
                    matrix_version=engagement.matrix_version,
                    identity=engagement.identity,
                    environment_summary={
                        **self.environment.render_summary(environment),
                        "target_scope": engagement.target_scope,
                        "monitored_permission_count": len(
                            state_result.monitored_permission_indices
                        ),
                        "unmonitored_permission_count": len(
                            state_result.unmonitored_permission_indices
                        ),
                    },
                    relevant_technique_ids=remaining,
                    previous_rejections=tuple(feedback[-50:]),
                    maximum_proposals=self.proposals_per_round,
                )
                self._trace(
                    "proposal_round_started",
                    engagement_id=engagement_id,
                    cycle_round=cycle_round,
                    remaining_technique_count=len(remaining),
                    previous_rejection_count=len(proposal_request.previous_rejections),
                )
                batch = self.proposer.propose(proposal_request)
                engagement.proposal_round += 1
                self._trace(
                    "proposal_round_completed",
                    engagement_id=engagement_id,
                    cycle_round=cycle_round,
                    provider=batch.provider,
                    model=batch.model,
                    proposal_count=len(batch.proposals),
                )
                if not batch.proposals:
                    break

                for proposal in batch.proposals:
                    if len(accepted) >= 3:
                        break
                    rejection = self._resolve_proposal(
                        proposal, engagement, matrix.techniques, seen_candidates
                    )
                    seen_candidates.add(proposal.candidate_id)
                    if rejection is not None:
                        self._trace(
                            "proposal_rejected",
                            engagement_id=engagement_id,
                            candidate_id=proposal.candidate_id,
                            technique_id=proposal.technique_id,
                            reason=rejection,
                            stage="resolution",
                        )
                        feedback.append(rejection)
                        if proposal.technique_id in matrix.techniques:
                            excluded.add(proposal.technique_id)
                        continue
                    validation = self.validator.validate_candidate(
                        CandidateValidationRequest(
                            candidate_id=proposal.candidate_id,
                            technique_id=proposal.technique_id,
                            state=state,
                        )
                    )
                    if not validation.admissible:
                        self._trace(
                            "proposal_rejected",
                            engagement_id=engagement_id,
                            candidate_id=proposal.candidate_id,
                            technique_id=proposal.technique_id,
                            reason=validation.explanation,
                            stage="validation",
                            feasible=validation.feasible,
                            outside_loaded_coverage=validation.outside_loaded_coverage,
                            missing_permissions=validation.missing_permissions,
                            covered_permissions=validation.covered_permissions,
                        )
                        feedback.append(f"{proposal.technique_id}: {validation.explanation}")
                        excluded.add(proposal.technique_id)
                        continue
                    technique = matrix.techniques[proposal.technique_id]
                    ranked_proposal = proposal.model_copy(update={"rank": len(accepted) + 1})
                    accepted.append(
                        CandidateCard(
                            proposal=ranked_proposal,
                            validation=validation,
                            required_permissions=tuple(
                                matrix.permissions[index] for index in technique.required_indices
                            ),
                            technique_title=technique.title,
                            expected_capabilities=technique.grants,
                        )
                    )
                    self._trace(
                        "proposal_accepted",
                        engagement_id=engagement_id,
                        candidate_id=proposal.candidate_id,
                        technique_id=proposal.technique_id,
                        rank=len(accepted),
                        validator_result_id=validation.result_id,
                    )
                    excluded.add(proposal.technique_id)

            engagement.status = EngagementStatus.AWAITING_APPROVAL
            engagement.candidates = {card.proposal.candidate_id: card for card in accepted}
            engagement.excluded_technique_ids = tuple(sorted(excluded))
            engagement.rejection_feedback = tuple(feedback[-100:])
            self.repository.save(engagement)
            self.execution.authorize(self._authorization(engagement, enabled=True))
            self._publish(engagement, tuple(accepted))
            self._trace(
                "candidates_published",
                engagement_id=engagement_id,
                candidate_ids=[card.proposal.candidate_id for card in accepted],
                candidate_count=len(accepted),
                state_version=engagement.state_version,
            )
            return CycleResult(
                engagement_id=engagement.engagement_id,
                status=engagement.status,
                proposal_round=engagement.proposal_round,
                candidates=tuple(accepted),
                message=(
                    f"Published {len(accepted)} admissible candidate(s) for explicit "
                    "operator review."
                ),
            )

    def decide(self, decision: OperatorDecision) -> CycleResult:
        with self._lock:
            engagement = self.get(decision.engagement_id)
            self._require_active(engagement)
            self._trace(
                "operator_decision_received",
                engagement_id=decision.engagement_id,
                decision=decision.decision,
                candidate_id=decision.candidate_id,
                operator=decision.operator,
                state_version=decision.state_version,
                matrix_version=decision.matrix_version,
            )
            if engagement.status != EngagementStatus.AWAITING_APPROVAL:
                raise DataConsistencyError(
                    f"engagement is {engagement.status}, not awaiting operator approval"
                )
            environment = self.environment.current(engagement.engagement_id)
            if decision.state_version != environment.state_version:
                raise VersionConflictError("operator decision uses a stale environment state")
            if decision.matrix_version != engagement.matrix_version:
                raise VersionConflictError("operator decision uses a stale matrix snapshot")

            if decision.decision == DecisionKind.TERMINATE:
                engagement.status = EngagementStatus.TERMINATED
                engagement.candidates = {}
                self.repository.save(engagement)
                self.execution.authorize(self._authorization(engagement, enabled=False))
                self._publish(engagement, ())
                return self._result(engagement, "Engagement terminated without execution.")

            if decision.decision in {
                DecisionKind.REJECT_ALL,
                DecisionKind.REQUEST_ALTERNATIVES,
            }:
                reason = decision.reason or decision.decision.value
                engagement.rejection_feedback = tuple(
                    [*engagement.rejection_feedback, reason][-100:]
                )
                engagement.status = EngagementStatus.CREATED
                engagement.candidates = {}
                self.repository.save(engagement)
                return self.cycle(engagement.engagement_id)

            candidate = self._selected_candidate(engagement, decision)
            if decision.decision == DecisionKind.REJECT:
                engagement.candidates.pop(candidate.proposal.candidate_id)
                engagement.rejection_feedback = tuple(
                    [
                        *engagement.rejection_feedback,
                        f"{candidate.proposal.technique_id}: "
                        f"operator rejected: {decision.reason or 'no reason supplied'}",
                    ][-100:]
                )
                if not engagement.candidates:
                    engagement.status = EngagementStatus.CREATED
                    self.repository.save(engagement)
                    return self.cycle(engagement.engagement_id)
                self.repository.save(engagement)
                remaining = tuple(
                    sorted(
                        engagement.candidates.values(),
                        key=lambda card: card.proposal.rank,
                    )
                )
                self._publish(engagement, remaining)
                return self._result(
                    engagement,
                    "Candidate rejected; remaining candidates still require explicit review.",
                )

            return self._approve(engagement, candidate, decision, environment.state_version)

    def _approve(
        self,
        engagement: Engagement,
        candidate: CandidateCard,
        decision: OperatorDecision,
        current_state_version: str,
    ) -> CycleResult:
        state = self.environment.state_vector(
            engagement.engagement_id, engagement.identity, engagement.target_scope
        )
        validation = self.validator.validate_candidate(
            CandidateValidationRequest(
                candidate_id=candidate.proposal.candidate_id,
                technique_id=candidate.proposal.technique_id,
                state=state,
                matrix_version=engagement.matrix_version,
            )
        )
        self._trace(
            "candidate_revalidated",
            engagement_id=engagement.engagement_id,
            candidate_id=candidate.proposal.candidate_id,
            technique_id=candidate.proposal.technique_id,
            admissible=validation.admissible,
            validator_result_id=validation.result_id,
        )
        if not validation.admissible:
            engagement.status = EngagementStatus.CREATED
            engagement.candidates = {}
            engagement.rejection_feedback = tuple(
                [*engagement.rejection_feedback, validation.explanation][-100:]
            )
            self.repository.save(engagement)
            raise VersionConflictError(
                "selected candidate is no longer admissible and must be reproposed"
            )

        arguments: dict = {}
        approval = ApprovalRecord(
            approval_id=new_id("approval"),
            engagement_id=engagement.engagement_id,
            candidate_id=candidate.proposal.candidate_id,
            action_id=candidate.proposal.action_id,
            identity=candidate.proposal.identity,
            target=candidate.proposal.target,
            arguments_digest=stable_digest(arguments),
            validator_result_id=validation.result_id,
            state_version=current_state_version,
            matrix_version=engagement.matrix_version,
            operator=decision.operator,
        )
        request = ExecutionRequest(
            engagement_id=engagement.engagement_id,
            candidate_id=candidate.proposal.candidate_id,
            action_id=candidate.proposal.action_id,
            identity=candidate.proposal.identity,
            target=candidate.proposal.target,
            arguments=arguments,
            validator_result_id=validation.result_id,
            approval_id=approval.approval_id,
            state_version=current_state_version,
            matrix_version=engagement.matrix_version,
        )
        self.repository.record_approval(approval)
        self.execution.authorize(self._authorization(engagement, enabled=True))
        self.execution.register_approval(approval)
        engagement.status = EngagementStatus.EXECUTING
        engagement.pending_approval_id = approval.approval_id
        self.repository.save(engagement)
        self._trace(
            "execution_started",
            engagement_id=engagement.engagement_id,
            candidate_id=candidate.proposal.candidate_id,
            action_id=candidate.proposal.action_id,
            approval_id=approval.approval_id,
        )

        try:
            result = self.execution.execute(request)
            updated = self.environment.apply_observation(
                ApplyObservationRequest(
                    expected_state_version=current_state_version,
                    observation=result.observation,
                )
            )
        except Exception as error:
            self._trace(
                "execution_failed",
                level="error",
                engagement_id=engagement.engagement_id,
                candidate_id=candidate.proposal.candidate_id,
                error_type=type(error).__name__,
                error=str(error),
            )
            engagement.status = EngagementStatus.FAILED
            engagement.last_error = str(error)
            engagement.pending_approval_id = None
            self.repository.save(engagement)
            self.execution.authorize(self._authorization(engagement, enabled=False))
            raise
        engagement.state_version = updated.state_version
        self._trace(
            "environment_updated",
            engagement_id=engagement.engagement_id,
            execution_id=result.observation.execution_id,
            success=result.observation.success,
            previous_state_version=current_state_version,
            state_version=updated.state_version,
            gained_permissions=result.observation.gained_permissions,
            gained_capabilities=result.observation.gained_capabilities,
            discovered_resources=result.observation.discovered_resources,
        )
        engagement.pending_approval_id = None
        engagement.candidates = {}
        if not result.observation.success:
            engagement.status = EngagementStatus.FAILED
            engagement.last_error = result.observation.api_response_summary
            self.repository.save(engagement)
            self.execution.authorize(self._authorization(engagement, enabled=False))
            return self._result(engagement, "Approved action failed; engagement stopped.")

        engagement.status = EngagementStatus.CREATED
        engagement.excluded_technique_ids = ()
        engagement.rejection_feedback = ()
        self.repository.save(engagement)
        if self.auto_cycle_after_execution:
            return self.cycle(engagement.engagement_id)
        return self._result(
            engagement,
            "Approved action executed; automatic proposal cycling is disabled.",
        )

    @staticmethod
    def _resolve_proposal(
        proposal: Proposal,
        engagement: Engagement,
        techniques: dict,
        seen_candidates: set[str],
    ) -> str | None:
        if proposal.candidate_id in seen_candidates:
            return f"duplicate candidate identifier {proposal.candidate_id!r}"
        if proposal.technique_id not in techniques:
            return f"unknown technique identifier {proposal.technique_id!r}"
        if proposal.action_id != f"technique:{proposal.technique_id}":
            return f"unregistered action identifier {proposal.action_id!r}"
        if proposal.identity != engagement.identity:
            return f"proposal identity {proposal.identity!r} is not controlled"
        if not target_is_allowed(proposal.target, engagement.target_scope):
            return f"proposal target {proposal.target!r} is outside the authorized scope"
        return None

    @staticmethod
    def _selected_candidate(engagement: Engagement, decision: OperatorDecision) -> CandidateCard:
        if decision.candidate_id is None:
            raise DataConsistencyError("decision requires an explicit candidate")
        candidate = engagement.candidates.get(decision.candidate_id)
        if candidate is None:
            raise DataConsistencyError(
                f"candidate {decision.candidate_id!r} is not currently displayed"
            )
        return candidate

    def _publish(
        self, engagement: Engagement, candidates: tuple[CandidateCard, ...]
    ) -> CandidateSet:
        state = self.environment.state_vector(
            engagement.engagement_id, engagement.identity, engagement.target_scope
        )
        state_analysis = self.validator.validate_state(
            StateValidationRequest(state=state, matrix_version=engagement.matrix_version)
        )
        return self.launchpad.publish(
            CandidateSet(
                engagement_id=engagement.engagement_id,
                objective=engagement.objective,
                state_version=engagement.state_version,
                matrix_version=engagement.matrix_version,
                candidates=candidates,
                identity=engagement.identity,
                scope=engagement.target_scope,
                state_analysis=state_analysis,
            )
        )

    @staticmethod
    def _authorization(engagement: Engagement, *, enabled: bool) -> EngagementAuthorization:
        return EngagementAuthorization(
            engagement_id=engagement.engagement_id,
            allowed_targets=(engagement.target_scope,),
            state_version=engagement.state_version,
            matrix_version=engagement.matrix_version,
            enabled=enabled,
        )

    @staticmethod
    def _require_active(engagement: Engagement) -> None:
        if engagement.status in {
            EngagementStatus.TERMINATED,
            EngagementStatus.COMPLETED,
            EngagementStatus.FAILED,
        }:
            raise DataConsistencyError(f"engagement is terminal with status {engagement.status}")

    @staticmethod
    def _result(engagement: Engagement, message: str) -> CycleResult:
        candidates = tuple(
            sorted(engagement.candidates.values(), key=lambda card: card.proposal.rank)
        )
        return CycleResult(
            engagement_id=engagement.engagement_id,
            status=engagement.status,
            proposal_round=engagement.proposal_round,
            candidates=candidates,
            message=message,
        )

    def _trace(self, event: str, *, level: str = "info", **details) -> None:
        if self.trace is not None:
            self.trace.emit("green_agent", event, level=level, **details)
