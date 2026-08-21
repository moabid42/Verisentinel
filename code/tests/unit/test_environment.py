from pathlib import Path

import pytest

from core.config import Paths
from core.errors import VersionConflictError
from core.models import ExecutionObservation
from environment.brain import EnvironmentBrain
from environment.models import ApplyObservationRequest, InitializeEnvironmentRequest
from environment.repository import EnvironmentRepository
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository


def services(tmp_path: Path) -> tuple[IngestorService, EnvironmentBrain]:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    ingestor = IngestorService(paths=Paths(), repository=snapshots)
    brain = EnvironmentBrain(
        repository=EnvironmentRepository(tmp_path / "environment"), snapshots=snapshots
    )
    return ingestor, brain


def test_environment_versions_permission_state_after_observation(tmp_path: Path) -> None:
    ingestor, brain = services(tmp_path)
    matrix = ingestor.build()
    initial = brain.initialize(
        InitializeEnvironmentRequest(
            engagement_id="engagement",
            objective="reach a test objective",
            matrix_version=matrix.matrix_version,
            identity="operator@example.test",
            credential="credential-a",
            scope="projects/sandbox",
            permissions=("resourcemanager.projects.get",),
            source="fixture",
        )
    )
    updated = brain.apply_observation(
        ApplyObservationRequest(
            expected_state_version=initial.state_version,
            observation=ExecutionObservation(
                execution_id="execution",
                engagement_id="engagement",
                action_id="test-action",
                identity="operator@example.test",
                target="projects/sandbox",
                success=True,
                api_response_summary="simulated",
                gained_permissions=("storage.objects.get",),
                gained_capabilities=("read-storage",),
            ),
        )
    )

    assert updated.state_version != initial.state_version
    assert updated.capabilities == ("read-storage",)
    vector = brain.state_vector("engagement", "operator@example.test", "projects/sandbox")
    names = {matrix.permissions[index] for index in vector.permission_indices}
    assert names == {"resourcemanager.projects.get", "storage.objects.get"}
    assert brain.repository.get("engagement", initial.state_version) == initial


def test_stale_observation_is_rejected(tmp_path: Path) -> None:
    ingestor, brain = services(tmp_path)
    matrix = ingestor.build()
    brain.initialize(
        InitializeEnvironmentRequest(
            engagement_id="engagement",
            objective="test",
            matrix_version=matrix.matrix_version,
            identity="identity",
            credential="credential",
            scope="projects/sandbox",
            permissions=(),
            source="fixture",
        )
    )
    request = ApplyObservationRequest(
        expected_state_version="sha256:stale",
        observation=ExecutionObservation(
            execution_id="execution",
            engagement_id="engagement",
            action_id="action",
            identity="identity",
            target="projects/sandbox",
            success=True,
            api_response_summary="simulated",
        ),
    )
    with pytest.raises(VersionConflictError):
        brain.apply_observation(request)

