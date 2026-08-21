"""Scenario -> MatrixSnapshot builder (the Parser's knowledge plane).

The real ProposerService and ValidatorService operate on an immutable, permission-indexed
``MatrixSnapshot`` published through a ``SnapshotRepository``. This module builds one of those
directly from a self-contained scenario, so both services run unchanged with no IAMouflage
export. The permission universe is the union of every permission the scenario can ever touch
(starting state, technique required/footprint perms, detection perms, and every scripted
``gained_permissions``) so that indices stay valid for the whole run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.ids import new_id
from core.models import (
    DetectionDefinition,
    IngestionReport,
    MatrixSnapshot,
    TechniqueDefinition,
)
from core.security import stable_digest
from evaluation_pipeline.scenario import EvaluationScenario, ScenarioError
from ingestion.models import BuildSnapshotRequest
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository


@dataclass(frozen=True, slots=True)
class Knowledge:
    """Everything the orchestrator needs after parsing, keyed for fast lookup."""

    scenario: EvaluationScenario
    matrix: MatrixSnapshot
    snapshots: SnapshotRepository
    column_index: dict[str, int]

    @property
    def matrix_version(self) -> str:
        return self.matrix.matrix_version


def _permission_universe(scenario: EvaluationScenario) -> tuple[str, ...]:
    permissions: set[str] = set(scenario.environment.starting_permissions)
    for technique in scenario.techniques:
        permissions.update(technique.required_permissions)
        permissions.update(technique.effective_footprint)
    for detection in scenario.detections:
        permissions.update(detection.permissions)
    for step in scenario.solution:
        permissions.update(step.output.gained_permissions)
    return tuple(sorted(permissions))


def build_knowledge(scenario: EvaluationScenario, artifacts_directory: Path) -> Knowledge:
    permissions = _permission_universe(scenario)
    column_index = {permission: index for index, permission in enumerate(permissions)}

    def indices(values: tuple[str, ...]) -> tuple[int, ...]:
        return tuple(sorted(column_index[value] for value in values))

    detections = {
        detection.id: DetectionDefinition(
            detection_id=detection.id,
            title=detection.title or detection.id,
            source=detection.source,
            paradigm=detection.paradigm,
            permission_indices=indices(detection.permissions),
        )
        for detection in scenario.detections
    }
    coverage_indices = tuple(
        sorted({index for row in detections.values() for index in row.permission_indices})
    )
    techniques = {
        technique.id: TechniqueDefinition(
            technique_id=technique.id,
            title=technique.title or technique.id,
            tactic=technique.tactic,
            service=technique.service,
            required_indices=indices(technique.required_permissions),
            footprint_indices=indices(technique.effective_footprint),
            grants=technique.grants,
        )
        for technique in scenario.techniques
    }

    version_tag = f"scenario:{scenario.name}"
    report = IngestionReport(
        report_id=new_id("ingestion"),
        permission_count=len(permissions),
        detection_count=len(detections),
        technique_count=len(techniques),
        enabled_detection_count=len(detections),
    )
    content = {
        "source": version_tag,
        "permissions": permissions,
        "enabled_detection_ids": sorted(detections),
        "detections": {
            key: value.model_dump(mode="json") for key, value in sorted(detections.items())
        },
        "coverage_indices": coverage_indices,
        "techniques": {
            key: value.model_dump(mode="json") for key, value in sorted(techniques.items())
        },
    }
    snapshot = MatrixSnapshot(
        matrix_version=stable_digest(content),
        iam_dataset_version=version_tag,
        iamouflage_version=version_tag,
        permissions=permissions,
        enabled_detection_ids=tuple(sorted(detections)),
        detections=detections,
        coverage_indices=coverage_indices,
        techniques=techniques,
        report=report,
    )
    snapshots = SnapshotRepository(artifacts_directory / "snapshots")
    snapshots.publish(snapshot)
    return Knowledge(
        scenario=scenario,
        matrix=snapshot,
        snapshots=snapshots,
        column_index=column_index,
    )


def build_from_iamouflage(scenario: EvaluationScenario, artifacts_directory: Path) -> Knowledge:
    """Mode B: ingest the real IAMouflage manual (full technique + permission universe) with the
    detection corpus filtered to ``detection_provider``. The scenario supplies only the identity,
    objective, and ground-truth solution; the catalog and coverage come from IAMouflage.
    """
    provider = scenario.detection_provider
    snapshots = SnapshotRepository(artifacts_directory / "snapshots")
    ingestor = IngestorService(repository=snapshots)
    # strict=False so the full corpus still publishes despite benign ingestion warnings (empty
    # detection rows, permissions outside the resolved universe) that are not fatal for planning.
    matrix = ingestor.build(BuildSnapshotRequest(enabled_sources=(provider,), strict=False))

    missing = [step.command for step in scenario.solution if step.command not in matrix.techniques]
    if missing:
        raise ScenarioError(
            "solution references techniques absent from the IAMouflage catalog: "
            + ", ".join(sorted(set(missing)))
        )
    column_index = {permission: index for index, permission in enumerate(matrix.permissions)}
    return Knowledge(
        scenario=scenario,
        matrix=matrix,
        snapshots=snapshots,
        column_index=column_index,
    )
