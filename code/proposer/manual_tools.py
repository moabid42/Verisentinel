"""Read-only query tools over a MatrixSnapshot.

These back the *agentic* proposer: instead of being handed a fixed candidate slice, the model
calls these to retrieve exactly the techniques and detections it wants from the (large) IAMouflage
manual, given the environment and objective. Every result is drawn from the same immutable snapshot
the validator reasons over, so any technique_id the model later proposes is guaranteed to resolve.

The tools are deterministic and side-effect free; the validator, not these tools, decides
feasibility and detection coverage.
"""

from __future__ import annotations

import re

from core.models import DetectionDefinition, MatrixSnapshot, TechniqueDefinition

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class ManualQuery:
    """A thin, deterministic query surface over one MatrixSnapshot."""

    def __init__(self, matrix: MatrixSnapshot) -> None:
        self.matrix = matrix
        self._permissions = matrix.permissions

    # -- shaping ---------------------------------------------------------------
    def _names(self, indices: tuple[int, ...]) -> list[str]:
        return [self._permissions[index] for index in indices]

    def _technique_view(self, technique: TechniqueDefinition) -> dict:
        return {
            "technique_id": technique.technique_id,
            "title": technique.title,
            "tactic": technique.tactic,
            "service": technique.service,
            "required_permissions": self._names(technique.required_indices),
            "footprint_permissions": self._names(technique.footprint_indices),
            "grants": list(technique.grants),
        }

    def _detection_view(self, detection: DetectionDefinition) -> dict:
        return {
            "detection_id": detection.detection_id,
            "title": detection.title,
            "source": detection.source,
            "permissions": self._names(detection.permission_indices),
        }

    # -- tools -----------------------------------------------------------------
    def search_techniques(
        self,
        keywords: str = "",
        service: str = "",
        tactic: str = "",
        permission: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Find catalog techniques. Filters (service/tactic/permission) are exact; `permission`
        matches techniques that REQUIRE that permission to run (what the identity must already
        hold), not what the technique later emits. `keywords` ranks by token overlap against the
        id/title/tactic/service. Returns up to `limit` views."""
        query = _tokens(keywords)
        results: list[tuple[int, str, dict]] = []
        for technique in self.matrix.techniques.values():
            if service and technique.service != service:
                continue
            if tactic and technique.tactic != tactic:
                continue
            if permission and permission not in self._names(technique.required_indices):
                continue
            searchable = " ".join(
                (technique.title, technique.tactic, technique.service, technique.technique_id)
            )
            overlap = len(query & _tokens(searchable)) if query else 0
            results.append((overlap, technique.technique_id, self._technique_view(technique)))
        results.sort(key=lambda row: (-row[0], row[1]))
        return [view for _, _, view in results[: max(1, limit)]]

    def get_technique(self, technique_id: str) -> dict | None:
        """Return the full view of one technique, or None if the id is not in the catalog."""
        technique = self.matrix.techniques.get(technique_id)
        return self._technique_view(technique) if technique is not None else None

    def search_detections(
        self,
        permission: str = "",
        service: str = "",
        keywords: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Find loaded detections whose covered permissions match `permission` (exact) or `service`
        (prefix), optionally ranked by `keywords` against the title. Returns up to `limit` views."""
        query = _tokens(keywords)
        results: list[tuple[int, str, dict]] = []
        for detection in self.matrix.detections.values():
            names = self._names(detection.permission_indices)
            if permission and permission not in names:
                continue
            if service and not any(name.startswith(f"{service}.") for name in names):
                continue
            overlap = len(query & _tokens(detection.title)) if query else 0
            results.append((overlap, detection.detection_id, self._detection_view(detection)))
        results.sort(key=lambda row: (-row[0], row[1]))
        return [view for _, _, view in results[: max(1, limit)]]

    def list_services(self) -> list[str]:
        """The distinct services present in the technique catalog, sorted."""
        return sorted({technique.service for technique in self.matrix.techniques.values()})
