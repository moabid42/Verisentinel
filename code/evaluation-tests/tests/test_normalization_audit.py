from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from evaluation_tests.normalization_audit import (
    audit_normalized_records,
    collect_normalization_audit,
)

CODE_ROOT = Path(__file__).resolve().parents[2]


def _fixture():
    detections = [
        {
            "id": "rule-1",
            "source": "fixture",
            "paradigm": "event",
            "requirement": {
                "groups": [
                    [
                        {
                            "token": "compute.firewalls.insert",
                            "permissions": ["compute.firewalls.create"],
                            "kind": "rest_method",
                            "confidence": "exact",
                            "provenance": "method_permissions.json[compute.firewalls.insert]",
                            "pattern": None,
                        }
                    ]
                ],
                "excluded": [],
            },
            "unresolved_tokens": [],
            "threshold": None,
            "window": None,
            "covered_permissions": ["compute.firewalls.create"],
        }
    ]
    techniques = [
        {
            "id": "technique-1",
            "source": "fixture",
            "file": "fixture.md",
            "required_perms": ["compute.firewalls.create"],
            "optional_perms": [],
        }
    ]
    permissions = {"compute.firewalls.create"}
    methods = {"compute.firewalls.insert": ["compute.firewalls.create"]}
    return detections, techniques, permissions, methods


def test_fixture_audit_passes() -> None:
    detections, techniques, permissions, methods = _fixture()
    result = audit_normalized_records(detections, techniques, permissions, methods, {})
    assert result["status"] == "pass"
    assert result["reference_consistency"] == {
        "checked": 1,
        "agreements": 1,
        "scope": (
            "stored decisions with a directly named pinned permission, REST, or gRPC reference"
        ),
    }
    assert result["flattening"]["agreements"] == 1


def test_audit_detects_reference_and_flattening_corruption() -> None:
    detections, techniques, permissions, methods = _fixture()
    broken = deepcopy(detections)
    broken[0]["requirement"]["groups"][0][0]["permissions"] = []
    result = audit_normalized_records(broken, techniques, permissions, methods, {})
    assert result["status"] == "fail"
    assert {issue["code"] for issue in result["issues"]} == {
        "reference_mismatch",
        "flattening_mismatch",
    }


def test_full_frozen_corpus_and_planner_projection_pass() -> None:
    result = collect_normalization_audit(CODE_ROOT)
    records = result["normalized_records"]
    projection = result["planner_projection"]
    assert result["status"] == "pass"
    assert records["operation_occurrences"] == 418
    assert records["traceability"]["complete"] == 418
    assert records["flattening"] == {"records_checked": 192, "agreements": 192}
    assert (
        records["reference_consistency"]["checked"]
        == records["reference_consistency"]["agreements"]
    )
    assert projection["detection_rows_checked"] == 192
    assert projection["detection_row_agreements"] == 192
    assert projection["deterministic_rebuild"]
