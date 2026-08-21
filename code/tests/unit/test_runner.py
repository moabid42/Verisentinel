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
from execution.credentials import InMemoryCredentialVault
from runner.scenario import ScenarioError, load_scenario
from runner.terminal import parse_choice

ACCESS_TOKEN = "ya29.test-access-token-that-is-long-enough"


def write_scenario(path: Path, access_token: str = ACCESS_TOKEN) -> None:
    path.write_text(
        f"""name: test-scenario
objective: Evaluate storage paths
operator: human@example.test
target_scope: projects/security-sandbox
starting_service_account:
  identity: start@security-sandbox.iam.gserviceaccount.com
  access_token: {access_token}
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


def test_scenario_loads_access_token_as_a_secret(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)

    scenario = load_scenario(path)

    assert scenario.starting_service_account.access_token.get_secret_value() == ACCESS_TOKEN
    assert ACCESS_TOKEN not in scenario.model_dump_json()


def test_scenario_rejects_an_unedited_token_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path, "REPLACE_WITH_A_REAL_ACCESS_TOKEN")

    with pytest.raises(ScenarioError, match="placeholder"):
        load_scenario(path)


def test_credential_vault_returns_only_an_opaque_reference() -> None:
    vault = InMemoryCredentialVault()

    reference = vault.register_access_token("identity", ACCESS_TOKEN)

    assert ACCESS_TOKEN not in reference
    assert vault.resolve(reference) == ACCESS_TOKEN


def test_debug_trace_redacts_named_and_embedded_secrets(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    trace = DebugTrace(path, "run", secrets=(ACCESS_TOKEN,), echo=False)

    trace.emit(
        "test",
        "redaction",
        access_token=ACCESS_TOKEN,
        message=f"failed while using {ACCESS_TOKEN}",
        usage={"total_tokens": 42},
    )

    text = path.read_text(encoding="utf-8")
    record = json.loads(text)
    assert ACCESS_TOKEN not in text
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
