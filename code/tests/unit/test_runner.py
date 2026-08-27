import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

import runner.cli as cli_module
from core.models import (
    CandidateCard,
    CandidateValidationResult,
    DecisionKind,
    Proposal,
)
from core.tracing import DebugTrace
from execution.capsule.doctor import CapsuleCheck, CapsuleDoctorReport
from execution.capsule.setup import CapsuleBuildReport
from execution.credentials import CredentialInspection, CredentialSourceKind
from runner.application import CorpusStatus, PlannerRunRequest
from runner.cli import app, main
from runner.scenario import ScenarioError, load_scenario
from runner.terminal import parse_choice

TEST_SECRET = "synthetic-sensitive-value-for-redaction"
CLI = CliRunner()


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


def test_top_level_and_nested_help_are_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", TEST_SECRET)
    routes = (
        ("--help",),
        ("scenario", "--help"),
        ("scenario", "validate", "--help"),
        ("scenario", "run", "--help"),
        ("auth", "--help"),
        ("auth", "inspect", "--help"),
        ("corpus", "--help"),
        ("corpus", "build", "--help"),
        ("corpus", "status", "--help"),
        ("sandbox", "--help"),
        ("sandbox", "build", "--help"),
        ("sandbox", "doctor", "--help"),
        ("run", "--help"),
    )
    results = tuple(CLI.invoke(app, list(route)) for route in routes)
    top_level = results[0]

    assert top_level.exit_code == 0
    assert "scenario" in top_level.stdout
    assert "auth" in top_level.stdout
    assert "corpus" in top_level.stdout
    assert "sandbox" in top_level.stdout
    assert "run" in top_level.stdout
    assert ".env" not in top_level.stdout
    assert "validate" in results[1].stdout
    assert "run" in results[1].stdout
    assert all(result.exit_code == 0 for result in results)
    assert all(TEST_SECRET not in result.output for result in results)


def test_scenario_validate_route_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)

    result = CLI.invoke(app, ["scenario", "validate", str(path)])

    assert result.exit_code == 0
    assert "SCENARIO VALID" in result.stdout
    assert "test-scenario" in result.stdout
    assert "projects/security-sandbox" in result.stdout


def test_scenario_validate_rejects_missing_path() -> None:
    result = CLI.invoke(app, ["scenario", "validate", "missing.yaml"])

    assert result.exit_code == 2
    assert "INPUT ERROR" in result.output


def test_required_command_input_returns_two() -> None:
    auth = CLI.invoke(app, ["auth", "inspect"])
    scenario = CLI.invoke(app, ["scenario", "validate"])
    run = CLI.invoke(app, ["run", "--scenario", "scenario.yaml"])

    assert auth.exit_code == 2
    assert scenario.exit_code == 2
    assert run.exit_code == 2


def test_installed_entrypoint_translates_usage_error_to_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(["run", "--scenario", "scenario.yaml"])

    assert result == 2
    assert "Missing option" in capsys.readouterr().err


def test_auth_inspect_prints_only_public_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    monkeypatch.setattr(
        cli_module,
        "inspect_authentication",
        lambda source: CredentialInspection(
            source_kind=source.kind,
            principal="runner@example.test",
            expires_at=expires_at,
        ),
    )

    result = CLI.invoke(
        app,
        ["auth", "inspect", "--credential-source", "env:TEST_TOKEN"],
    )

    assert result.exit_code == 0
    assert "AUTHENTICATION" in result.stdout
    assert "env" in result.stdout
    assert "runner@example.test" in result.stdout
    assert expires_at.isoformat() in result.stdout
    assert "TEST_TOKEN" not in result.stdout


def test_corpus_routes_render_service_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available = CorpusStatus(
        available=True,
        matrix_version="sha256:matrix",
        permission_count=2,
        detection_count=3,
        technique_count=4,
    )
    monkeypatch.setattr(cli_module, "build_corpus", lambda: available)
    monkeypatch.setattr(
        cli_module,
        "read_corpus_status",
        lambda: CorpusStatus(available=False),
    )

    built = CLI.invoke(app, ["corpus", "build"])
    current = CLI.invoke(app, ["corpus", "status"])

    assert built.exit_code == 0
    assert "CORPUS  READY" in built.stdout
    assert "Matrix" in built.stdout
    assert "sha256:matrix" in built.stdout
    assert "Techniques" in built.stdout
    assert "4" in built.stdout
    assert current.exit_code == 0
    assert "CORPUS  NOT BUILT" in current.stdout


def test_sandbox_doctor_reports_each_readiness_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "inspect_sandbox",
        lambda: CapsuleDoctorReport(
            available=True,
            provider="capsule",
            detail="Local execution capsule is ready.",
            checks=(
                CapsuleCheck(
                    name="runtime",
                    passed=True,
                    detail="Docker daemon is available",
                ),
            ),
        ),
    )
    result = CLI.invoke(app, ["sandbox", "doctor"])

    assert result.exit_code == 0
    assert "Provider  capsule" in result.stdout
    assert "SANDBOX  READY" in result.stdout
    assert "PASS" in result.stdout
    assert "runtime" in result.stdout


def test_sandbox_build_reports_prepared_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "build_sandbox",
        lambda: CapsuleBuildReport(
            image="verisentinel-capsule@sha256:" + "a" * 64,
            network="verisentinel-capsule",
            network_created=True,
        ),
    )

    result = CLI.invoke(app, ["sandbox", "build"])

    assert result.exit_code == 0
    assert "SANDBOX  BUILT" in result.stdout
    assert "Network state" in result.stdout
    assert "created" in result.stdout


def test_run_routes_validated_input_to_application_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[PlannerRunRequest] = []

    def run(request: PlannerRunRequest) -> int:
        requests.append(request)
        return 0

    monkeypatch.setattr(cli_module, "run_planner", run)

    result = CLI.invoke(
        app,
        [
            "run",
            "--scenario",
            "scenario.yaml",
            "--credential-source",
            "adc",
            "--rebuild-snapshot",
            "--quiet-trace",
        ],
    )

    assert result.exit_code == 0
    assert len(requests) == 1
    assert requests[0].scenario_path == Path("scenario.yaml")
    assert requests[0].credential_source.kind == CredentialSourceKind.ADC
    assert requests[0].rebuild_snapshot
    assert requests[0].quiet_trace


def test_scenario_run_uses_positional_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[PlannerRunRequest] = []
    monkeypatch.setattr(
        cli_module,
        "run_planner",
        lambda request: requests.append(request) or 0,
    )

    result = CLI.invoke(
        app,
        ["scenario", "run", "scenario.yaml", "--credential-source", "adc"],
    )

    assert result.exit_code == 0
    assert requests[0].scenario_path == Path("scenario.yaml")


def test_known_application_failure_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> CorpusStatus:
        raise RuntimeError("synthetic corpus failure")

    monkeypatch.setattr(cli_module, "build_corpus", fail)

    result = CLI.invoke(app, ["corpus", "build"])

    assert result.exit_code == 1
    assert "APPLICATION FAILURE" in result.output
    assert "synthetic corpus failure" in result.output


def test_interruption_returns_130_without_implicit_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(request: PlannerRunRequest) -> int:
        del request
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "run_planner", interrupt)

    result = CLI.invoke(
        app,
        [
            "run",
            "--scenario",
            "scenario.yaml",
            "--credential-source",
            "adc",
        ],
    )

    assert result.exit_code == 130
    assert "No implicit approval or execution" in result.output


def test_runner_rejects_raw_credential_argument_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "run",
            "--scenario",
            "scenario.yaml",
            "--credential-source",
            TEST_SECRET,
        ]
    )

    assert result == 2
    assert TEST_SECRET not in capsys.readouterr().err


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
