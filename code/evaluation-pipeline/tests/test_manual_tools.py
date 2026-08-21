"""Unit tests for the read-only manual query tools (proposer/manual_tools.py).

Built against a tiny synthetic MatrixSnapshot so they are fast and deterministic (no ingestor).
"""

from __future__ import annotations

from evaluation_pipeline.knowledge import build_knowledge
from evaluation_pipeline.scenario import EvaluationScenario

from proposer.manual_tools import ManualQuery

SCENARIO = {
    "name": "tools-fixture",
    "objective": "Escalate via cloudbuild then read serial output.",
    "operator": "eval@local.test",
    "target_scope": "projects/unit-sandbox",
    "environment": {
        "identity": "start@unit-sandbox.iam.gserviceaccount.com",
        "starting_permissions": ["cloudbuild.builds.create", "iam.serviceAccounts.actAs"],
    },
    "techniques": [
        {
            "id": "privilege-escalation:cloudbuild:1",
            "title": "Cloud Build actAs escalation",
            "tactic": "privilege-escalation",
            "service": "cloudbuild",
            "required_permissions": ["cloudbuild.builds.create", "iam.serviceAccounts.actAs"],
        },
        {
            "id": "discovery:compute-serial:1",
            "title": "Read serial port output",
            "tactic": "discovery",
            "service": "compute",
            "required_permissions": ["compute.instances.getSerialPortOutput"],
        },
    ],
    "detections": [
        {
            "id": "d.serial",
            "title": "Serial read watched",
            "permissions": ["compute.instances.getSerialPortOutput"],
        },
    ],
    "solution": [{"command": "privilege-escalation:cloudbuild:1"}],
}


def _query(tmp_path):
    scenario = EvaluationScenario.model_validate(SCENARIO)
    knowledge = build_knowledge(scenario, tmp_path / "artifacts")
    return ManualQuery(knowledge.matrix)


def test_search_by_service(tmp_path):
    query = _query(tmp_path)
    hits = query.search_techniques(service="cloudbuild")
    assert [h["technique_id"] for h in hits] == ["privilege-escalation:cloudbuild:1"]
    assert "cloudbuild.builds.create" in hits[0]["required_permissions"]


def test_search_by_permission_matches_required(tmp_path):
    # `permission` selects techniques that REQUIRE the permission (not ones that emit it).
    query = _query(tmp_path)
    hits = query.search_techniques(permission="iam.serviceAccounts.actAs")
    assert [h["technique_id"] for h in hits] == ["privilege-escalation:cloudbuild:1"]
    # A permission no technique requires yields nothing.
    assert query.search_techniques(permission="compute.instances.list") == []


def test_search_by_keywords_ranks_overlap(tmp_path):
    query = _query(tmp_path)
    hits = query.search_techniques(keywords="serial port output")
    assert hits[0]["technique_id"] == "discovery:compute-serial:1"


def test_get_technique_returns_none_for_unknown(tmp_path):
    query = _query(tmp_path)
    assert query.get_technique("does.not.exist") is None
    assert query.get_technique("discovery:compute-serial:1")["service"] == "compute"


def test_search_detections_by_permission(tmp_path):
    query = _query(tmp_path)
    hits = query.search_detections(permission="compute.instances.getSerialPortOutput")
    assert [h["detection_id"] for h in hits] == ["d.serial"]
    assert hits[0]["source"] == "custom"


def test_list_services(tmp_path):
    query = _query(tmp_path)
    assert query.list_services() == ["cloudbuild", "compute"]
