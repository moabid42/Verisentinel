"""Orchestrator: the loop from the design diagram.

Per solution step it (2) holds the parsed knowledge and env state, then runs the agentic
proposer<->validator inner loop (3->5): the prompt builder shapes a ProposalRequest, the real
proposer ranks techniques, and the real validator judges each one. Rejected proposals (a
hallucinated permission we do not hold, or a footprint that touches a loaded detection) have
their reasons fed back into the next round's prompt, and the model re-proposes, until up to
`maximum_candidates` accepted plans exist or `maximum_rounds` runs out. Only accepted plans
leave the loop (6) and reach the evaluator, which checks them against the ground-truth solution
(7) and either applies the scripted output and advances, or logs and stops. No IAMouflage,
environment brain, green agent, or infrastructure is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.ids import new_id
from core.metrics import RunMetrics
from core.models import CandidateValidationRequest
from core.tracing import DebugTrace
from evaluation_pipeline.evaluator import Evaluator, ProposedPlan, StepVerdict
from evaluation_pipeline.knowledge import Knowledge
from evaluation_pipeline.prompt_builder import PromptBuilder
from evaluation_pipeline.state import EnvState
from proposer.service import ProposerService
from validator.service import ValidatorService


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    engagement_id: str
    passed: bool
    steps_followed: int
    total_steps: int
    verdicts: tuple[StepVerdict, ...]
    final_env: dict
    metrics: RunMetrics | None = None


class Orchestrator:
    def __init__(
        self,
        knowledge: Knowledge,
        proposer: ProposerService,
        validator: ValidatorService,
        *,
        trace: DebugTrace | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.proposer = proposer
        self.validator = validator
        self.trace = trace
        self.prompt_builder = PromptBuilder(knowledge)
        self.evaluator = Evaluator(knowledge.scenario.evaluation)
        self.metrics = RunMetrics()

    def _emit(self, event: str, **fields) -> None:
        if self.trace is not None:
            self.trace.emit("orchestrator", event, **fields)

    def run(self) -> EvaluationOutcome:
        scenario = self.knowledge.scenario
        settings = scenario.evaluation
        engagement_id = new_id("evaluation_engagement")
        env = EnvState.initial(scenario)
        verdicts: list[StepVerdict] = []
        self._emit(
            "engagement_started",
            engagement_id=engagement_id,
            objective=scenario.objective,
            matrix_version=self.knowledge.matrix_version,
            total_steps=len(scenario.solution),
            validator_engine=self.validator.engine,
            smt_cross_checked=self.validator.verify_equivalence,
            maximum_rounds=settings.maximum_rounds,
            maximum_candidates=settings.maximum_candidates,
        )

        step_budget = min(len(scenario.solution), settings.maximum_steps)
        for step_index in range(step_budget):
            step = scenario.solution[step_index]
            self._emit(
                "step_started",
                step_index=step_index,
                expected_command=step.command,
                held_permission_count=len(env.held_permissions),
            )
            step_metrics = self.metrics.open_step(step_index)
            published, all_plans = self._propose_and_validate(env, engagement_id, step_metrics)
            verdict = self.evaluator.evaluate(step_index, step, published, all_plans)
            verdicts.append(verdict)
            self._emit(
                "step_evaluated",
                step_index=step_index,
                expected_command=verdict.expected_command,
                followed=verdict.followed,
                reason=verdict.reason,
                published=[p.technique_id for p in published],
            )
            if not verdict.followed:
                self._emit("deviation_detected", step_index=step_index, reason=verdict.reason)
                if settings.stop_on_deviation:
                    break
                continue
            env.apply(step.command, step.output)
            self._emit(
                "output_applied",
                step_index=step_index,
                command=step.command,
                gained_permissions=list(step.output.gained_permissions),
                held_permission_count=len(env.held_permissions),
            )

        steps_followed = sum(1 for verdict in verdicts if verdict.followed)
        passed = steps_followed == len(scenario.solution)
        self._emit(
            "engagement_finished",
            engagement_id=engagement_id,
            passed=passed,
            steps_followed=steps_followed,
            total_steps=len(scenario.solution),
        )
        return EvaluationOutcome(
            engagement_id=engagement_id,
            passed=passed,
            steps_followed=steps_followed,
            total_steps=len(scenario.solution),
            verdicts=tuple(verdicts),
            final_env=env.snapshot(),
            metrics=self.metrics,
        )

    def _validate(self, proposal, state) -> ProposedPlan:
        result = self.validator.validate_candidate(
            CandidateValidationRequest(
                candidate_id=proposal.candidate_id,
                technique_id=proposal.technique_id,
                state=state,
                matrix_version=self.knowledge.matrix_version,
            )
        )
        return ProposedPlan.build(proposal, result)

    def _propose_and_validate(
        self, env: EnvState, engagement_id: str, step_metrics
    ) -> tuple[tuple[ProposedPlan, ...], tuple[ProposedPlan, ...]]:
        settings = self.knowledge.scenario.evaluation
        state = self.prompt_builder.state_vector(env, engagement_id)
        if not settings.require_admissible:
            return self._single_pass_ungated(env, state, engagement_id, step_metrics)

        matrix = self.knowledge.matrix
        all_plans: list[ProposedPlan] = []
        accepted: list[ProposedPlan] = []
        excluded: set[str] = set()
        seen: set[str] = set()
        feedback: list[str] = []

        # Agentic proposer<->validator loop: keep re-proposing (with rejection reasons fed
        # back to the model) until `maximum_candidates` accepted plans exist or rounds run out.
        for round_index in range(1, settings.maximum_rounds + 1):
            if len(accepted) >= settings.maximum_candidates:
                break
            remaining = tuple(sorted(set(matrix.techniques) - excluded))
            if not remaining:
                break
            request = self.prompt_builder.build(
                env,
                state,
                engagement_id,
                previous_rejections=tuple(feedback[-50:]),
                relevant_technique_ids=remaining,
                excluded_technique_ids=tuple(sorted(excluded)),
            )
            batch = self.proposer.propose(request)
            step_metrics.proposal_rounds += 1
            step_metrics.add_cost(self.proposer.last_proposal_cost)
            self._emit(
                "proposal_round",
                round=round_index,
                remaining_technique_count=len(remaining),
                previous_rejection_count=len(request.previous_rejections),
                # The exact rejection feedback fed back into THIS round's prompt, so the
                # proposer<->validator back-and-forth is readable from trace.jsonl alone.
                previous_rejections=list(request.previous_rejections),
                provider=batch.provider,
                model=batch.model,
                proposals=[p.technique_id for p in batch.proposals],
            )
            if not batch.proposals:
                break
            for proposal in batch.proposals:
                if len(accepted) >= settings.maximum_candidates:
                    break
                if proposal.technique_id in seen:
                    continue
                seen.add(proposal.technique_id)
                plan = self._validate(proposal, state)
                all_plans.append(plan)
                excluded.add(proposal.technique_id)
                # Raw validator verdict for this candidate (bitset result, Z3-cross-checked when
                # smt_cross_checked is true — the validator raises if the two engines disagree).
                self._emit(
                    "candidate_validated",
                    round=round_index,
                    technique_id=proposal.technique_id,
                    admissible=plan.admissible,
                    feasible=plan.feasible,
                    outside_coverage=plan.outside_coverage,
                    missing_permissions=list(plan.missing_permissions),
                    matching_detection_ids=list(plan.matching_detection_ids),
                    explanation=plan.explanation,
                    validator_engine=self.validator.engine,
                    smt_cross_checked=self.validator.verify_equivalence,
                )
                if plan.admissible:
                    accepted.append(replace(plan, rank=len(accepted) + 1))
                    step_metrics.accepted += 1
                    self._emit(
                        "proposal_accepted",
                        round=round_index,
                        technique_id=proposal.technique_id,
                        rank=len(accepted),
                        reason=plan.explanation,
                    )
                else:
                    fed_back = f"{proposal.technique_id}: {plan.explanation}"
                    feedback.append(fed_back)
                    step_metrics.rejected += 1
                    self._emit(
                        "proposal_rejected",
                        round=round_index,
                        technique_id=proposal.technique_id,
                        reason=plan.explanation,
                        feasible=plan.feasible,
                        outside_coverage=plan.outside_coverage,
                        missing_permissions=list(plan.missing_permissions),
                        matching_detection_ids=list(plan.matching_detection_ids),
                        # Verbatim string appended to previous_rejections for the next round.
                        fed_back_to_model=fed_back,
                    )

        step_metrics.proposed = len(all_plans)
        return tuple(accepted[: settings.maximum_candidates]), tuple(all_plans)

    def _single_pass_ungated(
        self, env: EnvState, state, engagement_id: str, step_metrics
    ) -> tuple[tuple[ProposedPlan, ...], tuple[ProposedPlan, ...]]:
        # require_admissible=False publishes the top-ranked plans regardless of the validator
        # verdict, isolating the proposer's ranking from the validator's gate (no re-proposal).
        request = self.prompt_builder.build(env, state, engagement_id)
        batch = self.proposer.propose(request)
        step_metrics.proposal_rounds += 1
        step_metrics.add_cost(self.proposer.last_proposal_cost)
        self._emit(
            "proposals_received",
            provider=batch.provider,
            model=batch.model,
            proposals=[p.technique_id for p in batch.proposals],
        )
        all_plans = [self._validate(proposal, state) for proposal in batch.proposals]
        pool = sorted(all_plans, key=lambda plan: plan.rank)
        settings = self.knowledge.scenario.evaluation
        published = tuple(pool[: settings.maximum_candidates])
        step_metrics.proposed = len(all_plans)
        step_metrics.accepted = len(published)
        return published, tuple(all_plans)
