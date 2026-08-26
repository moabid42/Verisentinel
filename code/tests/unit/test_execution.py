from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import Paths
from core.errors import AuthorizationError
from core.models import ApprovalRecord, ExecutionRequest, ExecutionSpec
from core.security import stable_digest
from execution.models import EngagementAuthorization
from execution.repository import ExecutionRepository
from execution.service import ExecutionService
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository


def execution_fixture(tmp_path: Path) -> tuple[ExecutionService, ExecutionRequest]:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    technique = next(iter(matrix.techniques.values()))
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
    )
    service = ExecutionService(
        repository=ExecutionRepository(tmp_path / "execution"),
        snapshots=snapshots,
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
    with pytest.raises(AuthorizationError, match="already been consumed"):
        service.execute(request)


def test_execution_rejects_target_outside_engagement_allow_list(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
    changed = request.model_copy(update={"target": "projects/another-project"})

    with pytest.raises(AuthorizationError, match="outside the engagement target allow-list"):
        service.execute(changed)


def test_execution_request_rejects_untyped_parameters(tmp_path: Path) -> None:
    _, request = execution_fixture(tmp_path)
    payload = request.model_dump(mode="json")
    payload["arguments"] = {"command": "arbitrary"}

    with pytest.raises(ValidationError, match="arguments.command"):
        ExecutionRequest.model_validate(payload)


def test_changing_approved_argument_digest_invalidates_execution(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
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


def test_changing_approved_credential_reference_invalidates_execution(
    tmp_path: Path,
) -> None:
    service, request = execution_fixture(tmp_path)
    changed = request.model_copy(update={"credential_ref": "run/other"})

    with pytest.raises(AuthorizationError, match="credential_ref"):
        service.execute(changed)


def test_execution_rejects_missing_credential_reference(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
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


def test_execution_rejects_stale_environment_version(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
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


def test_execution_rejects_unregistered_action(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
    changed_request = request.model_copy(update={"action_id": "shell:arbitrary"})
    changed_approval = service.repository.approval(request.approval_id).model_copy(
        update={"action_id": "shell:arbitrary"}
    )
    replacement = changed_approval.model_copy(update={"approval_id": "approval-unregistered"})
    changed_request = changed_request.model_copy(update={"approval_id": replacement.approval_id})
    service.register_approval(replacement)

    with pytest.raises(AuthorizationError, match="not registered"):
        service.execute(changed_request)


def test_execution_kill_switch_fails_closed(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
    service.enabled = False

    with pytest.raises(AuthorizationError, match="kill switch"):
        service.execute(request)


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
