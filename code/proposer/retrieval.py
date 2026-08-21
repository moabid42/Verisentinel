from __future__ import annotations

import re

from core.models import MatrixSnapshot, ProposalRequest, TechniqueDefinition

TOKEN = re.compile(r"[a-z0-9]+")


class TechniqueRetriever:
    def __init__(self, limit: int = 50) -> None:
        # Default candidate cap for a prompt. The offline pipeline raises this so the scripted
        # oracle can see the whole IAMouflage catalog; live Mode B uses tools instead of a slice.
        self.limit = limit

    def retrieve(
        self,
        request: ProposalRequest,
        matrix: MatrixSnapshot,
        limit: int | None = None,
    ) -> tuple[TechniqueDefinition, ...]:
        limit = self.limit if limit is None else limit
        requested = set(request.relevant_technique_ids)
        candidates = (
            technique
            for technique in matrix.techniques.values()
            if not requested or technique.technique_id in requested
        )
        objective_tokens = set(TOKEN.findall(request.objective.lower()))
        completed = set(request.environment_summary.get("completed_actions", ()))

        def score(technique: TechniqueDefinition) -> tuple[int, int, str]:
            searchable = " ".join(
                (technique.title, technique.tactic, technique.service, technique.technique_id)
            ).lower()
            overlap = len(objective_tokens.intersection(TOKEN.findall(searchable)))
            novelty = 0 if f"technique:{technique.technique_id}" in completed else 1
            return overlap, novelty, technique.technique_id

        ranked = sorted(candidates, key=score, reverse=True)
        return tuple(ranked[:limit])

