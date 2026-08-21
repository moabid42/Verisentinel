from __future__ import annotations

from pathlib import Path

from core.models import EnvironmentSnapshot
from core.persistence import JsonModelStore


class EnvironmentRepository:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _states(self, engagement_id: str) -> JsonModelStore[EnvironmentSnapshot]:
        return JsonModelStore(
            self.directory / engagement_id / "states", EnvironmentSnapshot
        )

    def _current(self, engagement_id: str) -> JsonModelStore[EnvironmentSnapshot]:
        return JsonModelStore(self.directory / engagement_id, EnvironmentSnapshot)

    def publish(self, snapshot: EnvironmentSnapshot) -> EnvironmentSnapshot:
        key = snapshot.state_version.removeprefix("sha256:")
        self._states(snapshot.engagement_id).put(key, snapshot)
        self._current(snapshot.engagement_id).put("current", snapshot)
        return snapshot

    def current(self, engagement_id: str) -> EnvironmentSnapshot:
        return self._current(engagement_id).get("current")

    def get(self, engagement_id: str, state_version: str) -> EnvironmentSnapshot:
        key = state_version.removeprefix("sha256:")
        return self._states(engagement_id).get(key)

