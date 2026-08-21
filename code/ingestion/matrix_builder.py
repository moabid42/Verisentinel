from __future__ import annotations

from collections import Counter

from core.errors import DataConsistencyError
from core.ids import new_id
from core.models import (
    DetectionDefinition,
    IngestionIssue,
    IngestionReport,
    MatrixSnapshot,
    TechniqueDefinition,
)
from core.security import stable_digest
from ingestion.iam_dataset import IAMDataset
from ingestion.iamouflage import IAMouflageSnapshot
from ingestion.models import BuildSnapshotRequest, PermissionResolution


class MatrixBuilder:
    def __init__(
        self,
        dataset: IAMDataset,
        iamouflage: IAMouflageSnapshot,
        resolution: PermissionResolution,
    ) -> None:
        self.dataset = dataset
        self.iamouflage = iamouflage
        self.resolution = resolution

    def build(self, request: BuildSnapshotRequest) -> MatrixSnapshot:
        permissions = tuple(sorted(set(self.dataset.permissions) | set(self.resolution.extensions)))
        column_index = {permission: index for index, permission in enumerate(permissions)}
        issues: list[IngestionIssue] = []

        self._validate_resolution(column_index, issues)
        selected_detections = self._select_detections(request, issues)
        detections = self._build_detection_rows(selected_detections, column_index, issues)
        techniques = self._build_techniques(column_index, issues)

        self._append_duplicate_id_issues(
            self.iamouflage.detections, "detection", issues
        )
        self._append_duplicate_id_issues(
            self.iamouflage.techniques, "technique", issues
        )

        coverage = tuple(
            sorted(
                {
                    index
                    for detection in detections.values()
                    for index in detection.permission_indices
                }
            )
        )
        report = IngestionReport(
            report_id=new_id("ingestion"),
            permission_count=len(permissions),
            detection_count=len(self.iamouflage.detections),
            technique_count=len(self.iamouflage.techniques),
            enabled_detection_count=len(detections),
            issues=tuple(issues),
        )
        if request.strict and report.errors:
            messages = "; ".join(issue.message for issue in report.errors[:10])
            raise DataConsistencyError(f"matrix snapshot was not published: {messages}")

        content = {
            "iam_dataset_version": self.dataset.version,
            "iamouflage_version": self.iamouflage.version,
            "permissions": permissions,
            "enabled_detection_ids": sorted(detections),
            "detections": {
                key: value.model_dump(mode="json") for key, value in sorted(detections.items())
            },
            "coverage_indices": coverage,
            "techniques": {
                key: value.model_dump(mode="json") for key, value in sorted(techniques.items())
            },
        }
        return MatrixSnapshot(
            matrix_version=stable_digest(content),
            iam_dataset_version=self.dataset.version,
            iamouflage_version=self.iamouflage.version,
            permissions=permissions,
            enabled_detection_ids=tuple(sorted(detections)),
            detections=detections,
            coverage_indices=coverage,
            techniques=techniques,
            report=report,
        )

    def _select_detections(
        self, request: BuildSnapshotRequest, issues: list[IngestionIssue]
    ) -> tuple[dict, ...]:
        known = {str(record.get("id", "")) for record in self.iamouflage.detections}
        requested = set(request.enabled_detection_ids)
        for unknown_id in sorted(requested - known):
            issues.append(
                IngestionIssue(
                    severity="error",
                    code="unknown_enabled_detection",
                    source_id=unknown_id,
                    message=f"enabled detection {unknown_id!r} is absent from IAMouflage",
                )
            )
        sources = set(request.enabled_sources)
        return tuple(
            record
            for record in self.iamouflage.detections
            if (not requested or record.get("id") in requested)
            and (not sources or record.get("source") in sources)
        )

    def _build_detection_rows(
        self,
        records: tuple[dict, ...],
        column_index: dict[str, int],
        issues: list[IngestionIssue],
    ) -> dict[str, DetectionDefinition]:
        rows: dict[str, DetectionDefinition] = {}
        for record in records:
            detection_id = str(record.get("id", ""))
            if not detection_id:
                issues.append(
                    IngestionIssue(
                        severity="error",
                        code="missing_detection_id",
                        source_id="<missing>",
                        message="IAMouflage detection has no identifier.",
                    )
                )
                continue
            raw_permissions = record.get("covered_permissions") or self._flatten_rule(record)
            indices = self._resolve_indices(detection_id, raw_permissions, column_index, issues)
            if not indices:
                issues.append(
                    IngestionIssue(
                        severity="warning",
                        code="empty_detection_row",
                        source_id=detection_id,
                        message=f"detection {detection_id!r} monitors no resolved permission",
                    )
                )
            rows[detection_id] = DetectionDefinition(
                detection_id=detection_id,
                title=str(record.get("title") or detection_id),
                source=str(record.get("source") or "unknown"),
                paradigm=str(record.get("paradigm") or "unknown"),
                permission_indices=indices,
            )
        return rows

    def _build_techniques(
        self, column_index: dict[str, int], issues: list[IngestionIssue]
    ) -> dict[str, TechniqueDefinition]:
        techniques: dict[str, TechniqueDefinition] = {}
        for record in self.iamouflage.techniques:
            technique_id = str(record.get("id", ""))
            if not technique_id:
                issues.append(
                    IngestionIssue(
                        severity="error",
                        code="missing_technique_id",
                        source_id="<missing>",
                        message="IAMouflage technique has no identifier.",
                    )
                )
                continue
            required = self._resolve_indices(
                technique_id, record.get("required_perms", ()), column_index, issues
            )
            optional = self._resolve_indices(
                technique_id, record.get("optional_perms", ()), column_index, issues
            )
            if not required:
                issues.append(
                    IngestionIssue(
                        severity="warning",
                        code="unsupported_empty_technique_requirements",
                        source_id=technique_id,
                        message=(
                            f"technique {technique_id!r} has no resolved required permissions "
                            "and was excluded from the executable catalog"
                        ),
                    )
                )
                continue
            techniques[technique_id] = TechniqueDefinition(
                technique_id=technique_id,
                title=str(record.get("title") or technique_id),
                tactic=str(record.get("tactic") or "unknown"),
                service=str(record.get("service") or "unknown"),
                required_indices=required,
                footprint_indices=required,
                optional_indices=optional,
                footprint_assumption="required_permissions",
                grants=tuple(sorted(set(record.get("grants", ())))),
            )
        return techniques

    def _resolve_indices(
        self,
        source_id: str,
        raw_permissions: list[str] | tuple[str, ...],
        column_index: dict[str, int],
        issues: list[IngestionIssue],
    ) -> tuple[int, ...]:
        indices: set[int] = set()
        for raw_permission in raw_permissions:
            permission = self.resolution.aliases.get(raw_permission, raw_permission)
            if permission not in column_index:
                issues.append(
                    IngestionIssue(
                        severity="error",
                        code="unknown_permission",
                        source_id=source_id,
                        permission=raw_permission,
                        message=(
                            f"{source_id!r} references {raw_permission!r}, which is not in the "
                            "IAM permission universe or its reviewed resolution overlay"
                        ),
                    )
                )
                continue
            indices.add(column_index[permission])
        return tuple(sorted(indices))

    def _validate_resolution(
        self, column_index: dict[str, int], issues: list[IngestionIssue]
    ) -> None:
        for source, target in self.resolution.aliases.items():
            if target not in column_index:
                issues.append(
                    IngestionIssue(
                        severity="error",
                        code="invalid_permission_alias",
                        source_id=source,
                        permission=target,
                        message=f"permission alias {source!r} points to unknown target {target!r}",
                    )
                )

    @staticmethod
    def _flatten_rule(record: dict) -> tuple[str, ...]:
        requirement = record.get("requirement") or {}
        return tuple(
            permission
            for group in requirement.get("groups", ())
            for operation in group
            for permission in operation.get("permissions", ())
        )

    def _append_duplicate_id_issues(
        self, records: tuple[dict, ...], kind: str, issues: list[IngestionIssue]
    ) -> None:
        for identifier in self._duplicates(str(record.get("id", "")) for record in records):
            issues.append(
                IngestionIssue(
                    severity="error",
                    code=f"duplicate_{kind}_id",
                    source_id=identifier,
                    message=f"IAMouflage contains a duplicate {kind} identifier.",
                )
            )

    @staticmethod
    def _duplicates(values) -> tuple[str, ...]:
        counts = Counter(values)
        return tuple(sorted(value for value, count in counts.items() if value and count > 1))
