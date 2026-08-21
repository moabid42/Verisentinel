"""Evaluator: compares the published plans against the ground-truth solution step.

For each step the orchestrator hands over the (up to N) admissible candidate plans plus the
full validated set for diagnostics. The evaluator decides whether the model *followed the
plan* for this step under the configured match policy:

  - top_ranked        the #1 admissible plan must equal the expected command
  - within_candidates the expected command must appear among the published plans

It never mutates state; the orchestrator applies the scripted output only when ``followed``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from core.models import CandidateValidationResult, Proposal
from evaluation_pipeline.scenario import EvaluationSettings, SolutionStep


@dataclass(frozen=True, slots=True)
class ProposedPlan:
    technique_id: str
    rank: int
    admissible: bool
    feasible: bool
    outside_coverage: bool
    matching_detection_ids: tuple[str, ...]
    rationale: str
    missing_permissions: tuple[str, ...] = ()
    explanation: str = ""

    @classmethod
    def build(cls, proposal: Proposal, result: CandidateValidationResult) -> ProposedPlan:
        return cls(
            technique_id=proposal.technique_id,
            rank=proposal.rank,
            admissible=result.admissible,
            feasible=result.feasible,
            outside_coverage=result.outside_loaded_coverage,
            matching_detection_ids=result.matching_detection_ids,
            rationale=proposal.rationale,
            missing_permissions=result.missing_permissions,
            explanation=result.explanation,
        )


@dataclass(frozen=True, slots=True)
class StepVerdict:
    step_index: int
    expected_command: str
    expected_description: str
    followed: bool
    reason: str
    published_plans: tuple[ProposedPlan, ...] = ()
    all_plans: tuple[ProposedPlan, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["published_plans"] = [asdict(p) for p in self.published_plans]
        data["all_plans"] = [asdict(p) for p in self.all_plans]
        return data


@dataclass
class Evaluator:
    settings: EvaluationSettings

    def evaluate(
        self,
        step_index: int,
        step: SolutionStep,
        published: tuple[ProposedPlan, ...],
        all_plans: tuple[ProposedPlan, ...],
    ) -> StepVerdict:
        expected = step.command

        def verdict(followed: bool, reason: str) -> StepVerdict:
            return StepVerdict(
                step_index=step_index,
                expected_command=expected,
                expected_description=step.description,
                followed=followed,
                reason=reason,
                published_plans=published,
                all_plans=all_plans,
            )

        if not published:
            proposed_ids = [p.technique_id for p in all_plans]
            if expected in proposed_ids:
                return verdict(
                    False,
                    f"expected {expected!r} was proposed but rejected by the validator "
                    "(not admissible), so nothing was published",
                )
            return verdict(
                False,
                "no admissible plan was published for this step",
            )

        if self.settings.match_policy == "top_ranked":
            top = published[0]
            if top.technique_id == expected:
                return verdict(True, f"top-ranked plan matched expected {expected!r}")
            return verdict(
                False,
                f"top-ranked plan was {top.technique_id!r}, expected {expected!r}",
            )

        # within_candidates
        if any(p.technique_id == expected for p in published):
            return verdict(True, f"expected {expected!r} appeared among published plans")
        return verdict(
            False,
            f"expected {expected!r} was not among the published plans "
            f"{[p.technique_id for p in published]}",
        )
