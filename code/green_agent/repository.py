from __future__ import annotations

from pathlib import Path

from core.models import ApprovalRecord, Engagement
from core.persistence import JsonModelStore


class GreenAgentRepository:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.engagements = JsonModelStore(directory / "engagements", Engagement)
        self.approvals = JsonModelStore(directory / "approvals", ApprovalRecord)

    def save(self, engagement: Engagement) -> Engagement:
        self.engagements.put(engagement.engagement_id, engagement)
        return engagement

    def get(self, engagement_id: str) -> Engagement:
        return self.engagements.get(engagement_id)

    def record_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        self.approvals.put(approval.approval_id, approval)
        return approval
