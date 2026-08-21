"""Corpus-wide integrity and projection audit for RQ2.

The audit deliberately distinguishes two claims:

* *traceability/integrity*: every stored normalization decision is inspectable and the
  flattened exports and planner matrix can be reconstructed from it; and
* *semantic correctness*: the source token truly denotes the recorded IAM permission in GCP.

The first claim can be checked exhaustively from the frozen artifacts.  For the second, this
module checks every decision that cites a pinned permission, REST-method, or gRPC reference
against that reference.  This is a reference-consistency check, not an independent review of the
upstream reference tables and must not be reported as real-world mapping accuracy.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from core.config import Paths
from ingestion.iam_dataset import IAMDataset
from ingestion.iamouflage import IAMouflageSnapshot
from ingestion.matrix_builder import MatrixBuilder
from ingestion.models import BuildSnapshotRequest, PermissionResolution

_VERSION_SEGMENT = re.compile(r"^v\d+[a-z0-9]*$|^v\*$|^\*$|^beta$|^alpha$", re.IGNORECASE)
_REFERENCE_KEY = re.compile(r"(?:method_permissions|rpc_methods)\.json\[([^\]]+)\]")
_ALIAS_TARGET = re.compile(r"permission alias .+ -> ([^ ]+)")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _fold_token(token: str) -> str:
    return ".".join(
        segment
        for segment in token.strip().lstrip(".").split(".")
        if segment and not _VERSION_SEGMENT.match(segment)
    ).lower()


def _operation_rows(detections: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(record.get("id", "")), operation)
        for record in detections
        for group in (record.get("requirement") or {}).get("groups", ())
        for operation in group
    ]


def audit_normalized_records(
    detections: list[dict[str, Any]],
    techniques: list[dict[str, Any]],
    permission_vocabulary: set[str],
    method_permissions: dict[str, list[str]],
    rpc_methods: dict[str, dict[str, Any]],
    permission_aliases: dict[str, str] | None = None,
    permission_extensions: set[str] | None = None,
) -> dict[str, Any]:
    """Audit normalized exports without rebuilding the planner matrix."""

    issues: list[dict[str, str]] = []
    permission_aliases = permission_aliases or {}
    permission_extensions = permission_extensions or set()
    accepted_permissions = permission_vocabulary | set(permission_aliases) | permission_extensions

    def issue(code: str, source_id: str, detail: str) -> None:
        issues.append({"code": code, "source_id": source_id, "detail": detail})

    detection_ids = [str(record.get("id", "")) for record in detections]
    technique_ids = [str(record.get("id", "")) for record in techniques]
    if len(detection_ids) != len(set(detection_ids)):
        issue("duplicate_detection_id", "<corpus>", "detection identifiers are not unique")
    if len(technique_ids) != len(set(technique_ids)):
        issue("duplicate_technique_id", "<corpus>", "technique identifiers are not unique")

    operations = _operation_rows(detections)
    kind_counts = Counter(str(operation.get("kind", "")) for _, operation in operations)
    confidence_counts = Counter(str(operation.get("confidence", "")) for _, operation in operations)
    reference_checked = 0
    reference_agreements = 0
    trace_complete = 0
    canonical_permission_occurrences = 0

    permission_folded = {permission.lower(): permission for permission in permission_vocabulary}
    for source_id, operation in operations:
        token = str(operation.get("token", "")).strip()
        kind = str(operation.get("kind", ""))
        confidence = str(operation.get("confidence", ""))
        provenance = str(operation.get("provenance", "")).strip()
        permissions = tuple(str(value) for value in operation.get("permissions", ()))
        pattern = operation.get("pattern")

        if token and kind and confidence and provenance:
            trace_complete += 1
        else:
            issue(
                "incomplete_trace",
                source_id,
                f"token={token!r}, kind={kind!r}, confidence={confidence!r}, "
                f"provenance={provenance!r}",
            )

        for permission in permissions:
            canonical_permission_occurrences += 1
            if permission not in accepted_permissions:
                issue(
                    "noncanonical_permission",
                    source_id,
                    f"{token!r} resolved to unknown permission {permission!r}",
                )

        expected: tuple[str, ...] | None = None
        if kind == "permission":
            reference_checked += 1
            alias = _ALIAS_TARGET.search(provenance)
            if alias:
                expected = (alias.group(1),)
            elif confidence == "exact":
                expected = (token,)
            else:
                folded = permission_folded.get(_fold_token(token))
                expected = (folded,) if folded else ()
        elif kind == "rest_method":
            match = _REFERENCE_KEY.search(provenance)
            if match:
                reference_checked += 1
                expected = tuple(method_permissions.get(match.group(1), ()))
        elif kind == "grpc_method":
            match = _REFERENCE_KEY.search(provenance)
            if match:
                reference_checked += 1
                expected = tuple(rpc_methods.get(match.group(1), {}).get("permissions", ()))
        elif kind == "k8s":
            if permissions and any(
                not permission.startswith("container.") for permission in permissions
            ):
                issue(
                    "invalid_k8s_projection",
                    source_id,
                    f"{token!r} projected outside container.*: {permissions!r}",
                )
        elif kind == "pattern":
            if not pattern:
                issue("missing_pattern", source_id, f"pattern token {token!r} has no regex")
            else:
                try:
                    matcher = re.compile(str(pattern), re.IGNORECASE)
                except re.error as error:
                    issue("invalid_pattern", source_id, f"{token!r}: {error}")
                else:
                    for permission in permissions:
                        if not matcher.search(permission):
                            issue(
                                "pattern_projection_mismatch",
                                source_id,
                                f"{permission!r} does not match {pattern!r}",
                            )
        elif kind == "unresolved":
            if permissions or pattern:
                issue(
                    "unresolved_has_projection",
                    source_id,
                    f"unresolved token {token!r} carries a permission or pattern",
                )

        if expected is not None:
            if tuple(sorted(permissions)) == tuple(sorted(expected)):
                reference_agreements += 1
            else:
                issue(
                    "reference_mismatch",
                    source_id,
                    f"{token!r}: stored={permissions!r}, reference={expected!r}",
                )

    flattening_agreements = 0
    unresolved_occurrences = 0
    for record in detections:
        source_id = str(record.get("id", ""))
        groups = (record.get("requirement") or {}).get("groups", ())
        flattened = tuple(
            sorted(
                {
                    str(permission)
                    for group in groups
                    for operation in group
                    for permission in operation.get("permissions", ())
                }
            )
        )
        stored = tuple(sorted(str(value) for value in record.get("covered_permissions", ())))
        if flattened == stored:
            flattening_agreements += 1
        else:
            issue(
                "flattening_mismatch",
                source_id,
                f"stored={stored!r}, reconstructed={flattened!r}",
            )

        unresolved_tokens = tuple(str(value) for value in record.get("unresolved_tokens", ()))
        unresolved_occurrences += len(unresolved_tokens)
        empty_tokens = {
            str(operation.get("token", ""))
            for group in groups
            for operation in group
            if not operation.get("permissions")
        }
        for token in unresolved_tokens:
            if token not in empty_tokens:
                issue(
                    "unresolved_token_not_retained",
                    source_id,
                    f"{token!r} is not represented by an empty operation record",
                )

    required_permission_occurrences = 0
    optional_permission_occurrences = 0
    for record in techniques:
        source_id = str(record.get("id", ""))
        required = tuple(str(value) for value in record.get("required_perms", ()))
        optional = tuple(str(value) for value in record.get("optional_perms", ()))
        required_permission_occurrences += len(required)
        optional_permission_occurrences += len(optional)
        if len(required) != len(set(required)) or len(optional) != len(set(optional)):
            issue("duplicate_technique_permission", source_id, "permission lists are not unique")
        for permission in required + optional:
            if permission not in accepted_permissions:
                issue(
                    "noncanonical_technique_permission",
                    source_id,
                    f"unknown permission {permission!r}",
                )
        if not record.get("source") or not record.get("file"):
            issue("missing_technique_provenance", source_id, "source or file is absent")

    paradigms = Counter(str(record.get("paradigm", "unknown")) for record in detections)
    loss = {
        "multiple_logical_groups": sum(
            len((record.get("requirement") or {}).get("groups", ())) > 1 for record in detections
        ),
        "correlation_rules": paradigms.get("correlation", 0),
        "ueba_rules": paradigms.get("ueba", 0),
        "threshold_rules": sum(record.get("threshold") is not None for record in detections),
        "windowed_rules": sum(record.get("window") is not None for record in detections),
        "rules_with_exclusions": sum(
            bool((record.get("requirement") or {}).get("excluded")) for record in detections
        ),
        "records_with_unresolved_tokens": sum(
            bool(record.get("unresolved_tokens")) for record in detections
        ),
        "unresolved_token_occurrences": unresolved_occurrences,
        "unique_unresolved_tokens": len(
            {str(token) for record in detections for token in record.get("unresolved_tokens", ())}
        ),
        "empty_flattened_rows": sum(not record.get("covered_permissions") for record in detections),
    }

    return {
        "status": "pass" if not issues else "fail",
        "detection_records": len(detections),
        "technique_records": len(techniques),
        "operation_occurrences": len(operations),
        "unique_operation_tokens": len({str(op.get("token", "")) for _, op in operations}),
        "operation_kinds": dict(sorted(kind_counts.items())),
        "operation_confidence": dict(sorted(confidence_counts.items())),
        "traceability": {
            "checked": len(operations),
            "complete": trace_complete,
        },
        "reference_consistency": {
            "checked": reference_checked,
            "agreements": reference_agreements,
            "scope": (
                "stored decisions with a directly named pinned permission, REST, or gRPC reference"
            ),
        },
        "canonical_permission_occurrences": canonical_permission_occurrences,
        "planner_resolution_overlay": {
            "aliases": len(permission_aliases),
            "extensions": len(permission_extensions),
            "occurrences_requiring_overlay": sum(
                permission in permission_aliases or permission in permission_extensions
                for _, operation in operations
                for permission in operation.get("permissions", ())
            )
            + sum(
                permission in permission_aliases or permission in permission_extensions
                for record in techniques
                for permission in (
                    tuple(record.get("required_perms", ()))
                    + tuple(record.get("optional_perms", ()))
                )
            ),
        },
        "flattening": {
            "records_checked": len(detections),
            "agreements": flattening_agreements,
        },
        "techniques": {
            "records_checked": len(techniques),
            "with_required_permissions": sum(
                bool(record.get("required_perms")) for record in techniques
            ),
            "required_permission_occurrences": required_permission_occurrences,
            "optional_permission_occurrences": optional_permission_occurrences,
        },
        "paradigms": dict(sorted(paradigms.items())),
        "projection_loss": loss,
        "issues": issues,
    }


def audit_planner_projection(code_root: Path) -> dict[str, Any]:
    """Rebuild the all-source planner matrix twice and compare it with the exports."""

    paths = Paths(repository=code_root.parent)
    resolution_path = code_root / "ingestion" / "config" / "permission_resolution.json"
    resolution = PermissionResolution.model_validate_json(
        resolution_path.read_text(encoding="utf-8")
    )
    iamouflage = IAMouflageSnapshot.load(paths.iamouflage_data)
    builder = MatrixBuilder(IAMDataset.load(paths.iam_dataset), iamouflage, resolution)
    first = builder.build(BuildSnapshotRequest(strict=False))
    second = builder.build(BuildSnapshotRequest(strict=False))

    issues: list[dict[str, str]] = []
    deterministic_rebuild = first.matrix_version == second.matrix_version
    if not deterministic_rebuild:
        issues.append(
            {
                "code": "nondeterministic_matrix_rebuild",
                "source_id": "<matrix>",
                "detail": f"first={first.matrix_version}, second={second.matrix_version}",
            }
        )
    permission_index = {permission: index for index, permission in enumerate(first.permissions)}
    records = {str(record.get("id", "")): record for record in iamouflage.detections}
    row_agreements = 0
    for detection_id, row in first.detections.items():
        expected_names = {
            resolution.aliases.get(str(permission), str(permission))
            for permission in records[detection_id].get("covered_permissions", ())
        }
        expected = tuple(sorted(permission_index[name] for name in expected_names))
        if expected == row.permission_indices:
            row_agreements += 1
        else:
            issues.append(
                {
                    "code": "planner_row_mismatch",
                    "source_id": detection_id,
                    "detail": f"stored={row.permission_indices!r}, reconstructed={expected!r}",
                }
            )

    aggregate = tuple(
        sorted(
            {
                index
                for detection in first.detections.values()
                for index in detection.permission_indices
            }
        )
    )
    if aggregate != first.coverage_indices:
        issues.append(
            {
                "code": "aggregate_coverage_mismatch",
                "source_id": "<matrix>",
                "detail": "coverage_indices is not the union of all detection rows",
            }
        )

    return {
        "status": "pass" if not issues else "fail",
        "matrix_version": first.matrix_version,
        "deterministic_rebuild": deterministic_rebuild,
        "permission_columns": len(first.permissions),
        "detection_rows_checked": len(first.detections),
        "detection_row_agreements": row_agreements,
        "detection_permission_edges": sum(
            len(row.permission_indices) for row in first.detections.values()
        ),
        "aggregate_coverage_permissions": len(first.coverage_indices),
        "technique_rows": len(first.techniques),
        "techniques_using_required_as_footprint": sum(
            technique.required_indices == technique.footprint_indices
            for technique in first.techniques.values()
        ),
        "ingestion_issues": dict(
            sorted(Counter(item.code for item in first.report.issues).items())
        ),
        "issues": issues,
    }


def collect_normalization_audit(code_root: Path) -> dict[str, Any]:
    data = code_root / "IAMouflage" / "code" / "data"
    reference = data / "reference"
    detections = _load(data / "detections.json")
    techniques = _load(data / "techniques.json")
    permissions = set(_load(reference / "permissions.json"))
    method_permissions = _load(reference / "method_permissions.json")
    rpc_methods = {
        key: value
        for key, value in _load(
            code_root / "IAMouflage" / "code" / "reference" / "rpc_methods.json"
        ).items()
        if not key.startswith("_")
    }
    resolution = PermissionResolution.model_validate_json(
        (code_root / "ingestion" / "config" / "permission_resolution.json").read_text(
            encoding="utf-8"
        )
    )
    records = audit_normalized_records(
        detections,
        techniques,
        permissions,
        method_permissions,
        rpc_methods,
        permission_aliases=resolution.aliases,
        permission_extensions=set(resolution.extensions),
    )
    projection = audit_planner_projection(code_root)
    return {
        "status": "pass" if records["status"] == projection["status"] == "pass" else "fail",
        "normalized_records": records,
        "planner_projection": projection,
    }
