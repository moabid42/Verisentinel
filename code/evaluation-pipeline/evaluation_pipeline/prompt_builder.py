"""Prompt Builder: turns the current knowledge + env state into a ProposalRequest.

This is the only place that decides what the model gets to see each round: the objective,
the identity, a summary of the current environment (held permissions, discovered resources,
capabilities, completed actions), and any prior rejections. The technique catalog itself is
attached downstream by the proposer from the matrix; here we only shape the request.
"""

from __future__ import annotations

from core.ids import new_id
from core.models import ProposalRequest, StateVector
from evaluation_pipeline.knowledge import Knowledge
from evaluation_pipeline.state import EnvState


class PromptBuilder:
    def __init__(self, knowledge: Knowledge) -> None:
        self.knowledge = knowledge

    def state_vector(self, env: EnvState, engagement_id: str) -> StateVector:
        column_index = self.knowledge.column_index
        indices = tuple(
            sorted(column_index[p] for p in env.held_permissions if p in column_index)
        )
        return StateVector(
            state_version=new_id("state"),
            engagement_id=engagement_id,
            matrix_version=self.knowledge.matrix_version,
            identity=self.knowledge.scenario.environment.identity,
            credential="evaluation-no-credential",
            scope=self.knowledge.scenario.target_scope,
            permission_indices=indices,
            source="evaluation",
        )

    def build(
        self,
        env: EnvState,
        state: StateVector,
        engagement_id: str,
        *,
        previous_rejections: tuple[str, ...] = (),
        relevant_technique_ids: tuple[str, ...] = (),
        excluded_technique_ids: tuple[str, ...] = (),
    ) -> ProposalRequest:
        scenario = self.knowledge.scenario
        environment_summary = {
            "target_scope": scenario.target_scope,
            "identity": scenario.environment.identity,
            "roles": list(scenario.environment.roles),
            "held_permissions": sorted(env.held_permissions),
            "discovered_resources": sorted(env.discovered_resources),
            "capabilities": sorted(env.capabilities),
            "completed_actions": list(env.completed_actions),
        }
        return ProposalRequest(
            engagement_id=engagement_id,
            objective=scenario.objective,
            state_version=state.state_version,
            matrix_version=self.knowledge.matrix_version,
            identity=scenario.environment.identity,
            environment_summary=environment_summary,
            relevant_technique_ids=relevant_technique_ids,
            excluded_technique_ids=excluded_technique_ids,
            previous_rejections=previous_rejections,
            maximum_proposals=scenario.evaluation.maximum_proposals,
        )
