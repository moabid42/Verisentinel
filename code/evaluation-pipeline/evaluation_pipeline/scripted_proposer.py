"""Offline stand-in for GeminiProposer, used by ``--offline`` and unit tests.

It satisfies the tiny contract ProposerService relies on: attributes ``model`` / ``last_model``
and a ``propose(request, candidates, matrix) -> GeminiChoices`` method. An optional ``oracle``
ranking (a global ordering of technique ids) lets it deterministically float the intended
solution commands to the top so the plumbing can be exercised end-to-end without a network
call. With no oracle it simply preserves the retriever's order.
"""

from __future__ import annotations

from core.models import MatrixSnapshot, ProposalRequest, TechniqueDefinition
from proposer.models import GeminiChoice, GeminiChoices


class ScriptedProposer:
    def __init__(
        self,
        *,
        oracle: tuple[str, ...] = (),
        model: str = "scripted-offline",
    ) -> None:
        self.model = model
        self.last_model = model
        self.oracle = tuple(oracle)

    def propose(
        self,
        request: ProposalRequest,
        candidates: tuple[TechniqueDefinition, ...],
        matrix: MatrixSnapshot,
    ) -> GeminiChoices:
        available = [technique.technique_id for technique in candidates]
        completed = {
            action.removeprefix("technique:")
            for action in request.environment_summary.get("completed_actions", ())
        }
        fresh = [tid for tid in available if tid not in completed]
        done = [tid for tid in available if tid in completed]
        if self.oracle:
            ordered = [tid for tid in self.oracle if tid in fresh]
            ordered += [tid for tid in fresh if tid not in self.oracle]
        else:
            ordered = fresh
        # Already-executed actions sink to the bottom, mirroring the retriever's novelty term.
        ordered += done
        chosen = ordered[: request.maximum_proposals]
        return GeminiChoices(
            decision_summary="scripted offline ranking (no model call)",
            choices=[
                GeminiChoice(technique_id=tid, rationale="scripted offline proposal")
                for tid in chosen
            ],
        )
