from pathlib import Path

from core.config import Paths
from ingestion.models import BuildSnapshotRequest
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository


def test_real_sources_build_a_deterministic_complete_snapshot(tmp_path: Path) -> None:
    service = IngestorService(
        paths=Paths(), repository=SnapshotRepository(tmp_path / "snapshots")
    )
    first = service.build(BuildSnapshotRequest())
    second = service.build(BuildSnapshotRequest())

    assert first.matrix_version == second.matrix_version
    assert len(first.permissions) >= 10_129
    assert len(first.detections) == 192
    assert len(first.techniques) == 278
    assert "gcp.discovery.enumerate-permissions" not in first.techniques
    assert any(
        issue.code == "unsupported_empty_technique_requirements"
        and issue.source_id == "gcp.discovery.enumerate-permissions"
        for issue in first.report.issues
    )
    assert first.coverage_indices == tuple(
        sorted(
            {
                index
                for detection in first.detections.values()
                for index in detection.permission_indices
            }
        )
    )
    assert not first.report.errors
    assert service.current().matrix_version == first.matrix_version


def test_detection_source_profile_changes_the_matrix_version(tmp_path: Path) -> None:
    service = IngestorService(
        paths=Paths(), repository=SnapshotRepository(tmp_path / "snapshots")
    )
    all_sources = service.build()
    sigma_only = service.build(BuildSnapshotRequest(enabled_sources=("sigma",)))

    assert set(sigma_only.enabled_detection_ids)
    assert all(row.source == "sigma" for row in sigma_only.detections.values())
    assert sigma_only.matrix_version != all_sources.matrix_version
