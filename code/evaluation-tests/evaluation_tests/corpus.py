"""Collect a reproducible corpus/snapshot inventory for RQ2."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from evaluation_pipeline.parser import parse


def collect_corpus_snapshot(scenario_path: Path) -> dict:
    """Build the pinned IAMouflage scenario and report projection-level counts."""
    with TemporaryDirectory(prefix="evaluation-corpus-") as temporary_directory:
        knowledge = parse(scenario_path, Path(temporary_directory))
    matrix = knowledge.matrix
    issue_counts = Counter(issue.code for issue in matrix.report.issues)
    detection_source_counts = Counter(row.source for row in matrix.detections.values())
    return {
        "scenario": knowledge.scenario.name,
        "matrix_version": matrix.matrix_version,
        "iam_dataset_version": matrix.iam_dataset_version,
        "iamouflage_version": matrix.iamouflage_version,
        "permission_columns": len(matrix.permissions),
        "source_detection_records": matrix.report.detection_count,
        "enabled_detection_rows": len(matrix.detections),
        "represented_coverage_permissions": len(matrix.coverage_indices),
        "source_technique_records": matrix.report.technique_count,
        "executable_techniques": len(matrix.techniques),
        "technique_requirement_edges": sum(
            len(technique.required_indices) for technique in matrix.techniques.values()
        ),
        "technique_footprint_edges": sum(
            len(technique.footprint_indices) for technique in matrix.techniques.values()
        ),
        "detection_permission_edges": sum(
            len(detection.permission_indices) for detection in matrix.detections.values()
        ),
        "detection_sources": dict(sorted(detection_source_counts.items())),
        "ingestion_issue_counts": dict(sorted(issue_counts.items())),
        "ingestion_issue_total": sum(issue_counts.values()),
    }
