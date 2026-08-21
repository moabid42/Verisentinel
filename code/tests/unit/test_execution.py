from pathlib import Path

import pytest

from core.config import Paths
from core.errors import AuthorizationError
from core.models import ApprovalRecord, ExecutionRequest
from core.security import stable_digest
from execution.models import EngagementAuthorization
from execution.repository import ExecutionRepository
from execution.service import ExecutionService
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository


def execution_fixture(tmp_path: Path):
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
    )
    approval = ApprovalRecord(
        approval_id=request.approval_id,
        engagement_id=request.engagement_id,
        candidate_id=request.candidate_id,
        action_id=request.action_id,
        identity=request.identity,
        target=request.target,
        arguments_digest=stable_digest(request.arguments),
        validator_result_id=request.validator_result_id,
        state_version=request.state_version,
        matrix_version=request.matrix_version,
        operator="operator@example.test",
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
    with pytest.raises(AuthorizationError, match="already been consumed"):
        service.execute(request)


def test_execution_rejects_target_outside_engagement_allow_list(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
    changed = request.model_copy(update={"target": "projects/another-project"})

    with pytest.raises(AuthorizationError, match="outside the engagement target allow-list"):
        service.execute(changed)


def test_changing_approved_arguments_invalidates_execution(tmp_path: Path) -> None:
    service, request = execution_fixture(tmp_path)
    changed = request.model_copy(update={"arguments": {"command": "arbitrary"}})

    with pytest.raises(AuthorizationError, match="arguments"):
        service.execute(changed)


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
