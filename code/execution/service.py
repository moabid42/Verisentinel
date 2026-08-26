from __future__ import annotations

import os
from threading import RLock

from core.config import Paths
from core.errors import AuthorizationError, DataConsistencyError, NotFoundError
from core.models import (
    ApprovalRecord,
    ExecutionObservation,
    ExecutionRequest,
    ExecutionResult,
    ExecutionSpec,
)
from core.tracing import DebugTrace
from execution.credentials import CredentialResolver
from execution.guardrails import verify_execution_authority
from execution.models import EngagementAuthorization, ExecutionAttempt, ExecutionRecord
from execution.provider import ExecutionProvider
from execution.registry import ActionRegistry
from execution.repository import ExecutionRepository
from execution.simulator import SimulatorExecutionProvider
from ingestion.snapshot import SnapshotRepository


class ExecutionService:
    def __init__(
        self,
        repository: ExecutionRepository | None = None,
        snapshots: SnapshotRepository | None = None,
        provider_impl: ExecutionProvider | None = None,
        paths: Paths | None = None,
        enabled: bool | None = None,
        provider: str | None = None,
        credential_resolver: CredentialResolver | None = None,
        trace: DebugTrace | None = None,
    ) -> None:
        paths = paths or Paths()
        self.repository = repository or ExecutionRepository(paths.runtime / "execution")
        self.snapshots = snapshots or SnapshotRepository(paths.artifacts / "snapshots")
        self.registry = ActionRegistry(self.snapshots)
        self.credential_resolver = credential_resolver or CredentialResolver()
        self.trace = trace
        selected_provider = provider or os.getenv("EXECUTION_PROVIDER", "simulator")
        if provider_impl is None:
            if selected_provider != "simulator":
                raise ValueError(
                    f"execution provider {selected_provider!r} is not registered"
                )
            provider_impl = SimulatorExecutionProvider()
        elif provider is not None and provider != provider_impl.name:
            raise ValueError(
                "execution provider selector does not match the supplied provider"
            )
        if provider_impl.name not in {"simulator", "evaluation", "gcp"}:
            raise ValueError(
                f"execution provider {provider_impl.name!r} is not registered"
            )
        self.execution_provider = provider_impl
        self.provider = provider_impl.name
        default_enabled = "true" if self.provider == "simulator" else "false"
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("EXECUTION_ENABLED", default_enabled).lower() == "true"
        )
        self._lock = RLock()

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
                approval = self.repository.approval(request.approval_id)
                authorization = self.repository.authorization(request.engagement_id)
            except NotFoundError as error:
                raise AuthorizationError(
                    "execution requires a registered approval and engagement authorization"
                ) from error
            verify_execution_authority(request, approval, authorization)
            action = self.registry.resolve(request)
            try:
                attempt = self.repository.reserve_attempt(
                    approval,
                    self.provider,
                )
            except DataConsistencyError as error:
                raise AuthorizationError(str(error)) from error
            result = self._invoke_provider(request, approval, action.spec, attempt)
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

    def _invoke_provider(
        self,
        request: ExecutionRequest,
        approval: ApprovalRecord,
        spec: ExecutionSpec,
        attempt: ExecutionAttempt,
    ) -> ExecutionResult:
        """Resolve credentials, invoke the provider, and persist one outcome."""
        failure_code = "credential_resolution_failed"
        try:
            with self.credential_resolver.resolve(
                spec.credential_ref,
                spec.identity,
            ) as credential_lease:
                failure_code = "provider_execution_failed"
                raw_observation = self.execution_provider.execute(
                    spec,
                    credential_lease,
                )
                failure_code = "provider_observation_invalid"
                observation = self._validate_observation(
                    raw_observation,
                    spec,
                    credential_lease.access_token,
                )
            result = ExecutionResult(
                observation=observation,
                provider=self.provider,
            )
            failure_code = "result_persistence_failed"
            self.repository.record(
                ExecutionRecord(
                    request=request,
                    approval=approval,
                    result=result,
                    spec=spec,
                )
            )
            self.repository.complete_attempt(
                attempt,
                execution_id=observation.execution_id,
            )
            return result
        except BaseException as error:
            self.repository.fail_attempt(attempt, failure_code=failure_code)
            if not isinstance(error, Exception):
                raise
            if (
                failure_code == "credential_resolution_failed"
                and isinstance(error, AuthorizationError)
            ):
                raise
            raise AuthorizationError("execution provider failed") from None

    @staticmethod
    def _validate_observation(
        observation: ExecutionObservation,
        spec: ExecutionSpec,
        access_token: str,
    ) -> ExecutionObservation:
        """Validate bounded provider output against the approved specification."""
        validated = ExecutionObservation.model_validate(
            observation.model_dump(mode="json")
        )
        expected = (
            spec.engagement_id,
            spec.action.action_id,
            spec.identity,
            spec.target,
        )
        actual = (
            validated.engagement_id,
            validated.action_id,
            validated.identity,
            validated.target,
        )
        if actual != expected:
            raise AuthorizationError(
                "provider observation does not match the approved specification"
            )
        if ExecutionService._contains_value(
            validated.model_dump(mode="python"),
            access_token,
        ):
            raise AuthorizationError("provider observation contains credential material")
        serialized = validated.model_dump_json().encode("utf-8")
        if len(serialized) > spec.output_limit_bytes:
            raise AuthorizationError("provider observation exceeds its output limit")
        return validated

    @staticmethod
    def _contains_value(value: object, expected: str) -> bool:
        """Return whether a nested provider value contains credential material."""
        if isinstance(value, str):
            return expected in value
        if isinstance(value, dict):
            return any(
                ExecutionService._contains_value(item, expected)
                for item in value.values()
            )
        if isinstance(value, (list, tuple)):
            return any(
                ExecutionService._contains_value(item, expected) for item in value
            )
        return False

    def get(self, execution_id: str) -> ExecutionRecord:
        return self.repository.get(execution_id)
