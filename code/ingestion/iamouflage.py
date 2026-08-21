from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import DataConsistencyError
from core.security import stable_digest


def _records(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        raise DataConsistencyError(f"required IAMouflage export is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DataConsistencyError(f"IAMouflage export must be an array of objects: {path}")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class IAMouflageSnapshot:
    detections: tuple[dict[str, Any], ...]
    techniques: tuple[dict[str, Any], ...]
    version: str

    @classmethod
    def load(cls, directory: Path) -> IAMouflageSnapshot:
        detections = _records(directory / "detections.json")
        techniques = _records(directory / "techniques.json")
        return cls(
            detections=detections,
            techniques=techniques,
            version=stable_digest({"detections": detections, "techniques": techniques}),
        )
