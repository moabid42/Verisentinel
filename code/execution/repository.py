from __future__ import annotations

from pathlib import Path
from threading import RLock

from core.errors import DataConsistencyError, NotFoundError
from core.ids import new_id
from core.models import ApprovalRecord, utc_now
from core.persistence import JsonModelStore
from execution.models import (
    EngagementAuthorization,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    ExecutionRecord,
)
from execution.provider import ExecutionProviderName


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
        self.attempts = JsonModelStore(directory / "attempts", ExecutionAttempt)
        self.attempts_by_approval = JsonModelStore(
            directory / "attempts-by-approval", ExecutionAttempt
        )
        self._lock = RLock()

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

    def reserve_attempt(
        self,
        approval: ApprovalRecord,
        provider: ExecutionProviderName,
    ) -> ExecutionAttempt:
        """Atomically consume an approval before credential resolution."""
        attempt = ExecutionAttempt(
            attempt_id=new_id("attempt"),
            approval_id=approval.approval_id,
            engagement_id=approval.engagement_id,
            provider=provider,
        )
        reservation = (
            self.attempts_by_approval.directory / f"{approval.approval_id}.json"
        )
        reservation.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock, reservation.open("x", encoding="utf-8") as stream:
                stream.write(attempt.model_dump_json(indent=2))
        except FileExistsError as error:
            raise DataConsistencyError(
                f"approval {approval.approval_id!r} has already been consumed"
            ) from error
        self.attempts.put(attempt.attempt_id, attempt)
        return attempt

    def complete_attempt(
        self,
        attempt: ExecutionAttempt,
        *,
        execution_id: str,
    ) -> ExecutionAttempt:
        """Finalize a reserved attempt after a result is persisted."""
        completed = attempt.model_copy(
            update={
                "status": ExecutionAttemptStatus.SUCCEEDED,
                "completed_at": utc_now(),
                "execution_id": execution_id,
            }
        )
        self._store_attempt(completed)
        return completed

    def fail_attempt(
        self,
        attempt: ExecutionAttempt,
        *,
        failure_code: str,
    ) -> ExecutionAttempt:
        """Finalize a reserved attempt without persisting provider details."""
        failed = attempt.model_copy(
            update={
                "status": ExecutionAttemptStatus.FAILED,
                "completed_at": utc_now(),
                "failure_code": failure_code,
            }
        )
        self._store_attempt(failed)
        return failed

    def attempt_by_approval(self, approval_id: str) -> ExecutionAttempt:
        return self.attempts_by_approval.get(approval_id)

    def _store_attempt(self, attempt: ExecutionAttempt) -> None:
        self.attempts.put(attempt.attempt_id, attempt)
        self.attempts_by_approval.put(attempt.approval_id, attempt)

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
