import json
from pathlib import Path

import pytest

from core.models import (
    CandidateCard,
    CandidateValidationResult,
    DecisionKind,
    Proposal,
)
from core.tracing import DebugTrace
from runner.scenario import ScenarioError, load_scenario
from runner.terminal import parse_choice

TEST_SECRET = "synthetic-sensitive-value-for-redaction"


def write_scenario(path: Path, credential_ref: str = "run/default") -> None:
    path.write_text(
        f"""name: test-scenario
objective: Evaluate storage paths
operator: human@example.test
target_scope: projects/security-sandbox
starting_service_account:
  identity: start@security-sandbox.iam.gserviceaccount.com
  credential_ref: {credential_ref}
  permissions:
    - storage.objects.get
""",
        encoding="utf-8",
    )


def candidate(identifier: str = "candidate") -> CandidateCard:
    return CandidateCard(
        proposal=Proposal(
            candidate_id=identifier,
            technique_id="technique-id",
            action_id="technique:technique-id",
            identity="start@security-sandbox.iam.gserviceaccount.com",
            target="projects/security-sandbox",
            rationale="model rationale",
            rank=1,
        ),
        validation=CandidateValidationResult(
            result_id="validation",
            candidate_id=identifier,
            technique_id="technique-id",
            admissible=True,
            feasible=True,
            outside_loaded_coverage=True,
            missing_permissions=(),
            covered_permissions=(),
            matching_detection_ids=(),
            state_version="state",
            matrix_version="matrix",
            explanation="admissible",
        ),
        required_permissions=("storage.objects.get",),
    )


def test_scenario_loads_opaque_credential_reference(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)

    scenario = load_scenario(path)

    assert scenario.starting_service_account.credential_ref == "run/default"
    assert scenario.model_dump(mode="json")["starting_service_account"] == {
        "identity": "start@security-sandbox.iam.gserviceaccount.com",
        "credential_ref": "run/default",
        "permissions": ["storage.objects.get"],
    }


def test_scenario_rejects_embedded_access_token(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        """name: test-scenario
objective: Evaluate storage paths
operator: human@example.test
target_scope: projects/security-sandbox
starting_service_account:
  identity: start@security-sandbox.iam.gserviceaccount.com
  credential_ref: run/default
  access_token: synthetic-token-must-not-be-loaded
  permissions:
    - storage.objects.get
""",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioError, match="access_token") as raised:
        load_scenario(path)
    assert "synthetic-token-must-not-be-loaded" not in str(raised.value)


def test_scenario_requires_credential_reference(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        """name: test-scenario
objective: Evaluate storage paths
operator: human@example.test
target_scope: projects/security-sandbox
starting_service_account:
  identity: start@security-sandbox.iam.gserviceaccount.com
  permissions:
    - storage.objects.get
""",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioError, match="credential_ref"):
        load_scenario(path)


@pytest.mark.parametrize("credential_ref", ["", "contains spaces", "env:RAW_VALUE"])
def test_scenario_rejects_invalid_credential_reference(
    tmp_path: Path,
    credential_ref: str,
) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path, credential_ref)

    with pytest.raises(ScenarioError, match="credential_ref"):
        load_scenario(path)


def test_debug_trace_redacts_named_and_embedded_secrets(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    trace = DebugTrace(path, "run", secrets=(TEST_SECRET,), echo=False)

    trace.emit(
        "test",
        "redaction",
        access_token=TEST_SECRET,
        message=f"failed while using {TEST_SECRET}",
        usage={"total_tokens": 42},
    )

    text = path.read_text(encoding="utf-8")
    record = json.loads(text)
    assert TEST_SECRET not in text
    assert record["details"]["access_token"] == "[REDACTED]"
    assert record["details"]["message"] == "failed while using [REDACTED]"
    assert record["details"]["usage"]["total_tokens"] == 42


def test_terminal_choice_never_defaults_to_an_approval() -> None:
    candidates = (candidate(),)

    assert parse_choice("1", candidates).decision == DecisionKind.APPROVE
    assert parse_choice("r1", candidates).decision == DecisionKind.REJECT
    assert parse_choice("a", candidates).decision == DecisionKind.REQUEST_ALTERNATIVES
    assert parse_choice("q", candidates).decision == DecisionKind.TERMINATE
    with pytest.raises(ValueError):
        parse_choice("", candidates)
