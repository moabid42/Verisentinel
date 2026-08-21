from __future__ import annotations

from pathlib import Path

from core.config import Paths
from core.models import MatrixSnapshot
from ingestion.iam_dataset import IAMDataset
from ingestion.iamouflage import IAMouflageSnapshot
from ingestion.matrix_builder import MatrixBuilder
from ingestion.models import BuildSnapshotRequest, PermissionResolution
from ingestion.snapshot import SnapshotRepository


class IngestorService:
    def __init__(
        self,
        paths: Paths | None = None,
        repository: SnapshotRepository | None = None,
        resolution_path: Path | None = None,
    ) -> None:
        self.paths = paths or Paths()
        self.repository = repository or SnapshotRepository(self.paths.artifacts / "snapshots")
        self.resolution_path = resolution_path or Path(__file__).parent / "config" / (
            "permission_resolution.json"
        )

    def build(self, request: BuildSnapshotRequest | None = None) -> MatrixSnapshot:
        request = request or BuildSnapshotRequest()
        resolution = PermissionResolution.model_validate_json(
            self.resolution_path.read_text(encoding="utf-8")
        )
        builder = MatrixBuilder(
            IAMDataset.load(self.paths.iam_dataset),
            IAMouflageSnapshot.load(self.paths.iamouflage_data),
            resolution,
        )
        return self.repository.publish(builder.build(request))

    def current(self) -> MatrixSnapshot:
        return self.repository.current()

    def get(self, matrix_version: str) -> MatrixSnapshot:
        return self.repository.get(matrix_version)

    def report(self, matrix_version: str):
        return self.get(matrix_version).report
