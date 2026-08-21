from __future__ import annotations

from pathlib import Path

from core.errors import NotFoundError
from core.models import MatrixSnapshot
from core.persistence import JsonModelStore


class SnapshotRepository:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._versions = JsonModelStore(directory / "versions", MatrixSnapshot)
        self._current = JsonModelStore(directory, MatrixSnapshot)

    def publish(self, snapshot: MatrixSnapshot) -> MatrixSnapshot:
        key = snapshot.matrix_version.removeprefix("sha256:")
        self._versions.put(key, snapshot)
        self._current.put("current", snapshot)
        return snapshot

    def current(self) -> MatrixSnapshot:
        return self._current.get("current")

    def get(self, matrix_version: str) -> MatrixSnapshot:
        key = matrix_version.removeprefix("sha256:")
        try:
            return self._versions.get(key)
        except NotFoundError:
            if matrix_version == "current":
                return self.current()
            raise

