from __future__ import annotations

from pathlib import Path

from core.persistence import JsonModelStore
from launchpad.models import CandidateSet, DecisionReceipt


class LaunchpadRepository:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.candidates = JsonModelStore(directory / "candidates", CandidateSet)
        self.decisions = JsonModelStore(directory / "decisions", DecisionReceipt)

    def publish_candidates(self, candidates: CandidateSet) -> CandidateSet:
        self.candidates.put(candidates.engagement_id, candidates)
        return candidates

    def get_candidates(self, engagement_id: str) -> CandidateSet:
        return self.candidates.get(engagement_id)

    def record_decision(self, receipt: DecisionReceipt) -> DecisionReceipt:
        self.decisions.put(receipt.decision_id, receipt)
        return receipt
