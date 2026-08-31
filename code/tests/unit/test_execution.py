import asyncio
import hashlib
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from core.config import Paths
from core.errors import AuthorizationError, DataConsistencyError
from core.models import (
    ApprovalRecord,
    ExecutionObservation,
    ExecutionRequest,
    ExecutionSpec,
)
from core.security import stable_digest
from execution.credentials import (
    CredentialLease,
    CredentialResolver,
    TokenMetadata,
    parse_credential_source,
)
from execution.models import (
    EngagementAuthorization,
    ExecutionAttemptStatus,
    ExecutionRecord,
)
from execution.repository import ExecutionRepository
from execution.service import ExecutionService
from execution.simulator import SimulatorExecutionProvider
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
ACCESS_TOKEN = "synthetic-provider-access-token"
PRINCIPAL = "operator@example.test"


class StaticTokenInspector:
    """Return fixed credential metadata for offline execution tests."""

    def __init__(
        self,
        principal: str = PRINCIPAL,
        expires_at: datetime = NOW + timedelta(minutes=10),
    ) -> None:
        self.metadata = TokenMetadata(principal=principal, expires_at=expires_at)

    def inspect(self, access_token: str) -> TokenMetadata:
        del access_token
        return self.metadata


class RecordingProvider:
    """Record provider inputs and optionally return a custom observation."""

    name: Literal["evaluation"] = "evaluation"

    def __init__(self) -> None:
        self.specs: list[ExecutionSpec] = []
        self.leases: list[CredentialLease] = []
        self.observation_update: dict[str, object] = {}
        self.error: BaseException | None = None

    def execute(
        self,
        spec: ExecutionSpec,
        credential_lease: CredentialLease,
    ) -> ExecutionObservation:
        self.specs.append(spec)
        self.leases.append(credential_lease)
        assert credential_lease.access_token == ACCESS_TOKEN
        if self.error is not None:
            raise self.error
        observation = SimulatorExecutionProvider().execute(spec, credential_lease)
        return observation.model_copy(update=self.observation_update)


def credential_resolver(
    *,
    principal: str = PRINCIPAL,
    expires_at: datetime = NOW + timedelta(minutes=10),
    credential_ref: str = "run/default",
) -> CredentialResolver:
    """Build one registered deterministic credential resolver."""
    resolver = CredentialResolver(
        environ={"EXECUTION_TOKEN": ACCESS_TOKEN},
        token_inspector=StaticTokenInspector(principal, expires_at),
        now=lambda: NOW,
    )
    resolver.register(
        credential_ref,
        parse_credential_source("env:EXECUTION_TOKEN"),
    )
    return resolver


def execution_fixture(
    tmp_path: Path,
    *,
    provider: RecordingProvider | None = None,
    resolver: CredentialResolver | None = None,
) -> tuple[ExecutionService, ExecutionRequest]:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    technique = next(iter(matrix.techniques.values()))
    artifact_path = tmp_path / "action.py"
    artifact_path.write_text("print('approved')\n", encoding="utf-8")
    artifact_digest = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    request = ExecutionRequest(
        engagement_id="engagement",
        candidate_id="candidate",
        action_id=f"technique:{technique.technique_id}",
        identity="operator@example.test",
        target="projects/sandbox",
        validator_result_id="validation",
        approval_id="approval",
        state_version="state",
        matrix_version=matrix.matrix_version,
        credential_ref="run/default",
        artifact_digest=artifact_digest,
        artifact_path=str(artifact_path),
    )
    approval = ApprovalRecord(
        approval_id=request.approval_id,
        engagement_id=request.engagement_id,
        candidate_id=request.candidate_id,
        action_id=request.action_id,
        identity=request.identity,
        target=request.target,
        arguments_digest=stable_digest(request.arguments.model_dump(mode="json")),
        validator_result_id=request.validator_result_id,
        state_version=request.state_version,
        matrix_version=request.matrix_version,
        operator="operator@example.test",
        credential_ref=request.credential_ref,
        artifact_digest=request.artifact_digest,
        artifact_path=request.artifact_path,
    )
    service = ExecutionService(
        repository=ExecutionRepository(tmp_path / "execution"),
        snapshots=snapshots,
        provider_impl=provider,
        credential_resolver=resolver or credential_resolver(),
        enabled=True,
    )
    service.authorize(
        EngagementAuthorization(
            engagement_id=request.engagement_id,
            allowed_targets=("projects/sandbox",),
            state_version=request.state_version,
            matrix_version=request.matrix_version,
        )
    )
    service.register_approval(approval)
    return service, request


def test_registered_approved_action_executes_once_in_simulator(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)

    result = service.execute(request)

    assert result.provider == "simulator"
    assert result.observation.action_id == request.action_id
    assert result.observation.target == "projects/sandbox"
    record = service.repository.by_approval(request.approval_id)
    assert record.spec is not None
    assert record.spec.credential_ref == "run/default"
    assert record.spec.action.action_id == request.action_id
    assert ExecutionSpec.model_validate_json(record.spec.model_dump_json()) == record.spec
    attempt = service.repository.attempt_by_approval(request.approval_id)
    assert attempt.status == ExecutionAttemptStatus.SUCCEEDED
    assert attempt.execution_id == result.observation.execution_id
    with pytest.raises(AuthorizationError, match="already been consumed"):
        service.execute(request)


def test_execution_rejects_target_outside_engagement_allow_list(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    changed = request.model_copy(update={"target": "projects/another-project"})

    with pytest.raises(AuthorizationError, match="outside the engagement target allow-list"):
        service.execute(changed)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_execution_request_rejects_untyped_parameters(tmp_path: Path) -> None:
    _, request = execution_fixture(tmp_path)
    payload = request.model_dump(mode="json")
    payload["arguments"] = {"command": "arbitrary"}

    with pytest.raises(ValidationError, match="arguments.command"):
        ExecutionRequest.model_validate(payload)


def test_changing_approved_argument_digest_invalidates_execution(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    changed_approval = service.repository.approval(request.approval_id).model_copy(
        update={
            "approval_id": "approval-changed-arguments",
            "arguments_digest": stable_digest({"unexpected": True}),
        }
    )
    changed_request = request.model_copy(
        update={"approval_id": changed_approval.approval_id}
    )
    service.register_approval(changed_approval)

    with pytest.raises(AuthorizationError, match="arguments"):
        service.execute(changed_request)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_changing_approved_artifact_invalidates_execution(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    changed = request.model_copy(
        update={"artifact_digest": "sha256:" + "f" * 64}
    )

    with pytest.raises(AuthorizationError, match="artifact_digest"):
        service.execute(changed)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_changing_approved_credential_reference_invalidates_execution(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    changed = request.model_copy(update={"credential_ref": "run/other"})

    with pytest.raises(AuthorizationError, match="credential_ref"):
        service.execute(changed)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_execution_rejects_missing_credential_reference(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    changed_approval = service.repository.approval(request.approval_id).model_copy(
        update={
            "approval_id": "approval-missing-credential",
            "credential_ref": "",
        }
    )
    changed_request = request.model_copy(
        update={
            "approval_id": changed_approval.approval_id,
            "credential_ref": "",
        }
    )
    service.register_approval(changed_approval)

    with pytest.raises(AuthorizationError, match="no credential reference"):
        service.execute(changed_request)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_execution_rejects_stale_environment_version(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    service.authorize(
        EngagementAuthorization(
            engagement_id=request.engagement_id,
            allowed_targets=("projects/sandbox",),
            state_version="newer-state",
            matrix_version=request.matrix_version,
        )
    )

    with pytest.raises(AuthorizationError, match="stale environment state"):
        service.execute(request)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_execution_rejects_stale_matrix_version(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    changed = request.model_copy(update={"matrix_version": "sha256:stale"})

    with pytest.raises(AuthorizationError, match="stale matrix snapshot"):
        service.execute(changed)

    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_execution_rejects_unregistered_action(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    changed_request = request.model_copy(update={"action_id": "shell:arbitrary"})
    changed_approval = service.repository.approval(request.approval_id).model_copy(
        update={"action_id": "shell:arbitrary"}
    )
    replacement = changed_approval.model_copy(update={"approval_id": "approval-unregistered"})
    changed_request = changed_request.model_copy(update={"approval_id": replacement.approval_id})
    service.register_approval(replacement)

    with pytest.raises(AuthorizationError, match="not registered"):
        service.execute(changed_request)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_execution_kill_switch_fails_closed(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)
    service.enabled = False

    with pytest.raises(AuthorizationError, match="kill switch"):
        service.execute(request)
    assert service.repository.attempts.list_keys() == ()
    assert not provider.specs


def test_execution_dispatches_typed_spec_through_provider(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, request = execution_fixture(tmp_path, provider=provider)

    result = service.execute(request)

    assert result.provider == "evaluation"
    assert provider.specs[0].credential_ref == request.credential_ref
    assert provider.specs[0].arguments.model_dump(mode="json") == {}
    assert provider.leases[0].closed
    with pytest.raises(AuthorizationError, match="already been consumed"):
        service.execute(request)
    assert len(provider.specs) == 1


@pytest.mark.parametrize(
    ("principal", "expires_at", "message"),
    [
        ("different@example.test", NOW + timedelta(minutes=10), "principal"),
        (PRINCIPAL, NOW - timedelta(seconds=1), "expired"),
    ],
)
def test_invalid_credential_consumes_approval_without_calling_provider(
    tmp_path: Path,
    principal: str,
    expires_at: datetime,
    message: str,
) -> None:
    provider = RecordingProvider()
    resolver = credential_resolver(principal=principal, expires_at=expires_at)
    service, request = execution_fixture(
        tmp_path,
        provider=provider,
        resolver=resolver,
    )

    with pytest.raises(AuthorizationError, match=message):
        service.execute(request)

    assert not provider.specs
    attempt = service.repository.attempt_by_approval(request.approval_id)
    assert attempt.status == ExecutionAttemptStatus.FAILED
    assert attempt.failure_code == "credential_resolution_failed"
    with pytest.raises(AuthorizationError, match="already been consumed"):
        service.execute(request)


def test_provider_failure_is_redacted_and_closes_lease(tmp_path: Path) -> None:
    provider = RecordingProvider()
    provider.error = RuntimeError(f"provider echoed {ACCESS_TOKEN}")
    service, request = execution_fixture(tmp_path, provider=provider)

    with pytest.raises(AuthorizationError, match="execution provider failed") as raised:
        service.execute(request)

    rendered = "".join(traceback.format_exception(raised.value))
    assert ACCESS_TOKEN not in rendered
    assert provider.leases[0].closed
    attempt = service.repository.attempt_by_approval(request.approval_id)
    assert attempt.status == ExecutionAttemptStatus.FAILED
    assert attempt.failure_code == "provider_execution_failed"
    persisted = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "execution").rglob("*.json")
    )
    assert ACCESS_TOKEN not in persisted


def test_provider_timeout_consumes_approval_and_closes_lease(tmp_path: Path) -> None:
    provider = RecordingProvider()
    provider.error = TimeoutError("provider deadline exceeded")
    service, request = execution_fixture(tmp_path, provider=provider)

    with pytest.raises(AuthorizationError, match="execution provider failed"):
        service.execute(request)

    assert provider.leases[0].closed
    attempt = service.repository.attempt_by_approval(request.approval_id)
    assert attempt.status == ExecutionAttemptStatus.FAILED
    with pytest.raises(AuthorizationError, match="already been consumed"):
        service.execute(request)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_provider_interruption_closes_lease_and_records_failure(
    tmp_path: Path,
    interruption: BaseException,
) -> None:
    provider = RecordingProvider()
    provider.error = interruption
    service, request = execution_fixture(tmp_path, provider=provider)

    with pytest.raises(type(interruption)):
        service.execute(request)

    assert provider.leases[0].closed
    attempt = service.repository.attempt_by_approval(request.approval_id)
    assert attempt.status == ExecutionAttemptStatus.FAILED
    assert attempt.failure_code == "provider_execution_failed"


def test_provider_observation_must_match_approved_specification(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider()
    provider.observation_update = {"target": "projects/unapproved"}
    service, request = execution_fixture(tmp_path, provider=provider)

    with pytest.raises(AuthorizationError, match="execution provider failed"):
        service.execute(request)

    attempt = service.repository.attempt_by_approval(request.approval_id)
    assert attempt.status == ExecutionAttemptStatus.FAILED
    assert attempt.failure_code == "provider_observation_invalid"
    assert service.repository.executions.list_keys() == ()


def test_provider_observation_is_bounded(tmp_path: Path) -> None:
    provider = RecordingProvider()
    provider.observation_update = {
        "discovered_resources": tuple("r" * 1000 for _ in range(70))
    }
    service, request = execution_fixture(tmp_path, provider=provider)

    with pytest.raises(AuthorizationError, match="execution provider failed"):
        service.execute(request)

    attempt = service.repository.attempt_by_approval(request.approval_id)
    assert attempt.failure_code == "provider_observation_invalid"


def test_provider_cannot_return_credential_material(tmp_path: Path) -> None:
    provider = RecordingProvider()
    provider.observation_update = {
        "api_response_summary": f"response included {ACCESS_TOKEN}"
    }
    service, request = execution_fixture(tmp_path, provider=provider)

    with pytest.raises(AuthorizationError, match="execution provider failed"):
        service.execute(request)

    persisted = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "execution").rglob("*.json")
    )
    assert ACCESS_TOKEN not in persisted


@pytest.mark.parametrize("provider_name", ["unregistered", "evaluation", "gcp"])
def test_unavailable_provider_never_falls_back_to_simulator(
    tmp_path: Path,
    provider_name: str,
) -> None:
    with pytest.raises(ValueError, match="not registered"):
        ExecutionService(
            repository=ExecutionRepository(tmp_path / "execution"),
            snapshots=SnapshotRepository(tmp_path / "snapshots"),
            provider=provider_name,
        )


def test_capsule_provider_is_selected_only_after_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution.capsule.doctor import CapsuleDoctor
    from execution.capsule.provider import CapsuleConfiguration

    image = (
        "verisentinel-capsule@sha256:"
        "d4670ddecec6eb9df14b990c03aa258dece2078f8114401c7f2086e4c4089ffe"
    )
    configuration = CapsuleConfiguration(
        image=image,
        network="private-mock-network",
        user_id=1000,
        group_id=1000,
    )
    monkeypatch.setattr(
        CapsuleConfiguration,
        "from_environment",
        classmethod(lambda cls: configuration),
    )
    checked: list[bool] = []
    monkeypatch.setattr(
        CapsuleDoctor,
        "require_ready",
        lambda self: checked.append(True),
    )

    service = ExecutionService(
        repository=ExecutionRepository(tmp_path / "execution"),
        snapshots=SnapshotRepository(tmp_path / "snapshots"),
        provider="capsule",
    )

    assert service.provider == "capsule"
    assert checked == [True]
    assert not service.enabled


def test_unready_capsule_provider_fails_during_assembly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution.capsule.doctor import CapsuleDoctor

    def unavailable(self) -> None:
        del self
        raise ValueError("execution provider 'capsule' is unavailable")

    monkeypatch.setattr(CapsuleDoctor, "require_ready", unavailable)

    with pytest.raises(ValueError, match="capsule.*unavailable"):
        ExecutionService(
            repository=ExecutionRepository(tmp_path / "execution"),
            snapshots=SnapshotRepository(tmp_path / "snapshots"),
            provider="capsule",
        )


def test_provider_selector_must_match_injected_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match"):
        ExecutionService(
            repository=ExecutionRepository(tmp_path / "execution"),
            snapshots=SnapshotRepository(tmp_path / "snapshots"),
            provider="simulator",
            provider_impl=RecordingProvider(),
        )


def test_injected_provider_name_must_be_registered(tmp_path: Path) -> None:
    class UnregisteredProvider:
        name = "unregistered"

        def execute(
            self,
            spec: ExecutionSpec,
            credential_lease: CredentialLease,
        ) -> ExecutionObservation:
            raise AssertionError("unregistered provider must not execute")

    with pytest.raises(ValueError, match="not registered"):
        ExecutionService(
            repository=ExecutionRepository(tmp_path / "execution"),
            snapshots=SnapshotRepository(tmp_path / "snapshots"),
            provider_impl=UnregisteredProvider(),
        )


def test_approval_reservation_is_atomic_across_repository_instances(
    tmp_path: Path,
) -> None:
    service, request = execution_fixture(tmp_path)
    approval = service.repository.approval(request.approval_id)
    competing_repository = ExecutionRepository(tmp_path / "execution")

    service.repository.reserve_attempt(approval, "simulator")

    with pytest.raises(DataConsistencyError, match="already been consumed"):
        competing_repository.reserve_attempt(approval, "simulator")


def test_empty_legacy_argument_records_still_validate(tmp_path: Path) -> None:
    _, request = execution_fixture(tmp_path)
    request_payload = request.model_dump(mode="json")
    request_payload.pop("credential_ref")
    approval_payload = {
        "approval_id": "legacy-approval",
        "engagement_id": request.engagement_id,
        "candidate_id": request.candidate_id,
        "action_id": request.action_id,
        "identity": request.identity,
        "target": request.target,
        "arguments_digest": stable_digest({}),
        "validator_result_id": request.validator_result_id,
        "state_version": request.state_version,
        "matrix_version": request.matrix_version,
        "operator": "operator@example.test",
    }

    legacy_request = ExecutionRequest.model_validate(request_payload)
    legacy_approval = ApprovalRecord.model_validate(approval_payload)

    assert legacy_request.credential_ref == ""
    assert legacy_request.arguments.model_dump(mode="json") == {}
    assert legacy_approval.credential_ref == ""


def test_legacy_execution_record_without_spec_still_validates(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
    service.execute(request)
    record = service.repository.by_approval(request.approval_id)
    payload = record.model_dump(mode="json")
    payload.pop("spec")

    legacy_record = ExecutionRecord.model_validate(payload)

    assert legacy_record.spec is None
    assert legacy_record.result == record.result
