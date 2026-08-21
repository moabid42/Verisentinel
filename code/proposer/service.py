from __future__ import annotations

from core.config import Paths
from core.errors import DataConsistencyError
from core.ids import new_id
from core.metrics import ProposalCost
from core.models import Proposal, ProposalBatch, ProposalRequest, TechniqueDefinition
from ingestion.snapshot import SnapshotRepository
from proposer.gemini import GeminiProposer
from proposer.retrieval import TechniqueRetriever


class ProposerService:
    def __init__(
        self,
        snapshots: SnapshotRepository | None = None,
        retriever: TechniqueRetriever | None = None,
        gemini: GeminiProposer | None = None,
        paths: Paths | None = None,
        agentic: bool = False,
    ) -> None:
        paths = paths or Paths()
        self.snapshots = snapshots or SnapshotRepository(paths.artifacts / "snapshots")
        self.retriever = retriever or TechniqueRetriever()
        self.gemini = gemini or GeminiProposer()
        # Agentic mode: skip retrieval and let the backend retrieve from the manual with tools
        # (IAMouflage mode). Requires a backend exposing ``propose_with_tools``.
        self.agentic = agentic and hasattr(self.gemini, "propose_with_tools")
        # Cost of the most recent propose() call, aggregated per state by the orchestrator.
        # None for backends (e.g. the offline scripted oracle) that do not report cost.
        self.last_proposal_cost: ProposalCost | None = None

    def propose(self, request: ProposalRequest) -> ProposalBatch:
        self.last_proposal_cost = None
        matrix = self.snapshots.get(request.matrix_version)
        if self.agentic:
            choices = self.gemini.propose_with_tools(request, matrix)
            known = dict(matrix.techniques)
        else:
            candidates = self.retriever.retrieve(request, matrix)
            if not candidates:
                return ProposalBatch(
                    proposals=(),
                    provider="gemini",
                    model=getattr(self.gemini, "last_model", self.gemini.model),
                )
            choices = self.gemini.propose(request, candidates, matrix)
            known = {technique.technique_id: technique for technique in candidates}
        self.last_proposal_cost = getattr(self.gemini, "last_proposal_cost", None)
        chosen_ids = [choice.technique_id for choice in choices.choices]
        unknown = [technique_id for technique_id in chosen_ids if technique_id not in known]
        if unknown:
            raise DataConsistencyError(
                f"proposer returned technique IDs outside its catalog: {', '.join(unknown)}"
            )
        if len(chosen_ids) != len(set(chosen_ids)):
            raise DataConsistencyError("proposer returned duplicate technique IDs")
        selected = tuple(
            (known[choice.technique_id], choice.rationale) for choice in choices.choices
        )[: request.maximum_proposals]

        target = str(
            request.environment_summary.get("target_scope")
            or request.environment_summary.get("scope")
            or ""
        )
        proposals = tuple(
            self._proposal(request, technique, rationale, target, rank)
            for rank, (technique, rationale) in enumerate(selected, start=1)
        )
        return ProposalBatch(
            proposals=proposals,
            provider="gemini",
            model=getattr(self.gemini, "last_model", self.gemini.model),
        )

    @staticmethod
    def _proposal(
        request: ProposalRequest,
        technique: TechniqueDefinition,
        rationale: str,
        target: str,
        rank: int,
    ) -> Proposal:
        return Proposal(
            candidate_id=new_id("candidate"),
            technique_id=technique.technique_id,
            action_id=f"technique:{technique.technique_id}",
            identity=request.identity,
            target=target,
            rationale=rationale,
            rank=rank,
        )
