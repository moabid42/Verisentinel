from __future__ import annotations

import os
from threading import RLock

from core.config import Paths
from core.errors import AuthorizationError, DataConsistencyError, NotFoundError
from core.models import ApprovalRecord, ExecutionRequest, ExecutionResult
from core.tracing import DebugTrace
from execution.credentials import InMemoryCredentialVault
from execution.guardrails import verify_execution_authority
from execution.models import EngagementAuthorization, ExecutionRecord
from execution.registry import ActionRegistry
from execution.repository import ExecutionRepository
from execution.simulator import IAMSimulator
from ingestion.snapshot import SnapshotRepository


class ExecutionService:
    def __init__(
        self,
        repository: ExecutionRepository | None = None,
        snapshots: SnapshotRepository | None = None,
        simulator: IAMSimulator | None = None,
        paths: Paths | None = None,
        enabled: bool | None = None,
        provider: str | None = None,
        credentials: InMemoryCredentialVault | None = None,
        trace: DebugTrace | None = None,
    ) -> None:
        paths = paths or Paths()
        self.repository = repository or ExecutionRepository(paths.runtime / "execution")
        self.snapshots = snapshots or SnapshotRepository(paths.artifacts / "snapshots")
        self.registry = ActionRegistry(self.snapshots)
        self.simulator = simulator or IAMSimulator()
        self.credentials = credentials or InMemoryCredentialVault()
        self.trace = trace
        self.provider = provider or os.getenv("EXECUTION_PROVIDER", "simulator")
        if self.provider != "simulator":
            raise ValueError(
                "only the offline simulator is implemented; GCP actions require an explicit "
                "registered provider"
            )
        default_enabled = "true" if self.provider == "simulator" else "false"
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("EXECUTION_ENABLED", default_enabled).lower() == "true"
        )
        self._lock = RLock()

    def register_access_token(self, identity: str, access_token: str) -> str:
        reference = self.credentials.register_access_token(identity, access_token)
        if self.trace is not None:
            self.trace.emit(
                "execution",
                "credential_registered",
                identity=identity,
                credential_reference=reference,
            )
        return reference

    def authorize(
        self, authorization: EngagementAuthorization
    ) -> EngagementAuthorization:
        return self.repository.authorize(authorization)

    def register_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        return self.repository.register_approval(approval)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        with self._lock:
            if self.trace is not None:
                self.trace.emit(
                    "execution",
                    "request_received",
                    engagement_id=request.engagement_id,
                    candidate_id=request.candidate_id,
                    action_id=request.action_id,
                    identity=request.identity,
                    target=request.target,
                    provider=self.provider,
                )
            if not self.enabled:
                raise AuthorizationError("the execution service kill switch is active")
            try:
                self.repository.by_approval(request.approval_id)
            except NotFoundError:
                pass
            else:
                raise AuthorizationError(
                    f"approval {request.approval_id!r} has already been consumed"
                )

            try:
                approval = self.repository.approval(request.approval_id)
                authorization = self.repository.authorization(request.engagement_id)
            except NotFoundError as error:
                raise AuthorizationError(
                    "execution requires a registered approval and engagement authorization"
                ) from error
            verify_execution_authority(request, approval, authorization)
            action = self.registry.resolve(request)
            observation = self.simulator.execute(request, action)
            result = ExecutionResult(observation=observation, provider="simulator")
            try:
                self.repository.record(
                    ExecutionRecord(request=request, approval=approval, result=result)
                )
            except DataConsistencyError as error:
                raise AuthorizationError(str(error)) from error
            if self.trace is not None:
                self.trace.emit(
                    "execution",
                    "request_completed",
                    execution_id=result.observation.execution_id,
                    success=result.observation.success,
                    provider=result.provider,
                    response_summary=result.observation.api_response_summary,
                )
            return result

    def get(self, execution_id: str) -> ExecutionRecord:
        return self.repository.get(execution_id)
