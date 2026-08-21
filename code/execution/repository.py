from __future__ import annotations

from pathlib import Path

from core.errors import DataConsistencyError, NotFoundError
from core.models import ApprovalRecord
from core.persistence import JsonModelStore
from execution.models import EngagementAuthorization, ExecutionRecord


class ExecutionRepository:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.authorizations = JsonModelStore(
            directory / "authorizations", EngagementAuthorization
        )
        self.approvals = JsonModelStore(directory / "approvals", ApprovalRecord)
        self.executions = JsonModelStore(directory / "executions", ExecutionRecord)
        self.executions_by_approval = JsonModelStore(
            directory / "executions-by-approval", ExecutionRecord
        )

    def authorize(self, authorization: EngagementAuthorization) -> EngagementAuthorization:
        self.authorizations.put(authorization.engagement_id, authorization)
        return authorization

    def authorization(self, engagement_id: str) -> EngagementAuthorization:
        return self.authorizations.get(engagement_id)

    def register_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        try:
            existing = self.approvals.get(approval.approval_id)
        except NotFoundError:
            self.approvals.put(approval.approval_id, approval)
            return approval
        if existing != approval:
            raise DataConsistencyError(
                f"approval {approval.approval_id!r} cannot be replaced with different content"
            )
        return existing

    def approval(self, approval_id: str) -> ApprovalRecord:
        return self.approvals.get(approval_id)

    def record(self, record: ExecutionRecord) -> ExecutionRecord:
        try:
            previous = self.executions_by_approval.get(record.approval.approval_id)
        except NotFoundError:
            pass
        else:
            raise DataConsistencyError(
                f"approval {record.approval.approval_id!r} was already consumed by "
                f"execution {previous.result.observation.execution_id!r}"
            )
        execution_id = record.result.observation.execution_id
        self.executions.put(execution_id, record)
        self.executions_by_approval.put(record.approval.approval_id, record)
        return record

    def get(self, execution_id: str) -> ExecutionRecord:
        return self.executions.get(execution_id)

    def by_approval(self, approval_id: str) -> ExecutionRecord:
        return self.executions_by_approval.get(approval_id)
