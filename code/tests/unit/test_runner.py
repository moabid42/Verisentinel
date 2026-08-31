import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

import runner.cli as cli_module
from core.models import (
    ActionArtifact,
    ActionCommand,
    CandidateCard,
    CandidateValidationResult,
    DecisionKind,
    ExecutionObservation,
    Proposal,
    ReviewStage,
)
from core.tracing import DebugTrace
from execution.capsule.doctor import CapsuleCheck, CapsuleDoctorReport
from execution.capsule.setup import CapsuleBuildReport
from execution.credentials import CredentialInspection, CredentialSourceKind
from runner.application import ConnectedEnvironment, CorpusStatus, PlannerRunRequest
from runner.cli import app, main
from runner.connection import ConnectionMode, SandboxConnection
from runner.infrastructure import DevelopmentInfrastructure, InfrastructureStatus
from runner.scenario import ScenarioError, load_scenario
from runner.session import ShellSessionRepository
from runner.terminal import TerminalUI, parse_choice

TEST_SECRET = "synthetic-sensitive-value-for-redaction"
CLI = CliRunner()


def write_scenario(path: Path, credential_ref: str = "run/default") -> None:
    path.write_text(
        f"""name: test-scenario
objective: Evaluate storage paths
operator: human@example.test
target_scope: projects/security-sandbox
infrastructure:
  path: projects/security-sandbox/buckets/scenario-target
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
    assert scenario.infrastructure.path == (
        "projects/security-sandbox/buckets/scenario-target"
    )
    assert scenario.model_dump(mode="json")["starting_service_account"] == {
        "identity": "start@security-sandbox.iam.gserviceaccount.com",
        "credential_ref": "run/default",
        "permissions": ["storage.objects.get"],
    }


def test_scenario_loads_safe_relative_terraform_root(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)
    document = path.read_text(encoding="utf-8").replace(
        "  path: projects/security-sandbox/buckets/scenario-target",
        "  path: projects/security-sandbox/buckets/scenario-target\n"
        "  terraform_root: terraform",
    )
    path.write_text(document, encoding="utf-8")

    scenario = load_scenario(path)

    assert scenario.infrastructure.terraform_root == "terraform"


@pytest.mark.parametrize("terraform_root", ["/tmp/module", "../module", "terraform root"])
def test_scenario_rejects_unsafe_terraform_root(
    tmp_path: Path,
    terraform_root: str,
) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)
    document = path.read_text(encoding="utf-8").replace(
        "  path: projects/security-sandbox/buckets/scenario-target",
        "  path: projects/security-sandbox/buckets/scenario-target\n"
        f"  terraform_root: {terraform_root}",
    )
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ScenarioError, match="terraform_root"):
        load_scenario(path)


def test_scenario_rejects_embedded_access_token(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        """name: test-scenario
objective: Evaluate storage paths
operator: human@example.test
target_scope: projects/security-sandbox
infrastructure:
  path: projects/security-sandbox/buckets/scenario-target
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
infrastructure:
  path: projects/security-sandbox/buckets/scenario-target
starting_service_account:
  identity: start@security-sandbox.iam.gserviceaccount.com
  permissions:
    - storage.objects.get
""",
        encoding="utf-8",
    )

    with pytest.raises(ScenarioError, match="credential_ref"):
        load_scenario(path)


def test_scenario_requires_canonical_infrastructure_path(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)
    document = path.read_text(encoding="utf-8").replace(
        "projects/security-sandbox/buckets/scenario-target",
        "https://storage.googleapis.com/scenario-target",
    )
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ScenarioError, match="infrastructure.path"):
        load_scenario(path)


def test_scenario_requires_service_account_starting_identity(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)
    document = path.read_text(encoding="utf-8").replace(
        "start@security-sandbox.iam.gserviceaccount.com",
        "human@example.test",
    )
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ScenarioError, match="starting_service_account.identity"):
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
        ("shell", "--help"),
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
        ("sandbox", "connect", "--help"),
        ("sandbox", "doctor", "--help"),
        ("sandbox", "status", "--help"),
        ("sandbox", "disconnect", "--help"),
        ("env", "--help"),
        ("env", "show", "--help"),
        ("env", "analyse", "--help"),
        ("infra", "--help"),
        ("infra", "create", "--help"),
        ("infra", "destroy", "--help"),
        ("infra", "list", "--help"),
        ("infra", "show", "--help"),
        ("session", "--help"),
        ("session", "list", "--help"),
        ("session", "show", "--help"),
        ("session", "resume", "--help"),
        ("run", "--help"),
    )
    results = tuple(CLI.invoke(app, list(route)) for route in routes)
    top_level = results[0]

    assert top_level.exit_code == 0
    assert "scenario" in top_level.stdout
    assert "auth" in top_level.stdout
    assert "corpus" in top_level.stdout
    assert "sandbox" in top_level.stdout
    assert "env" in top_level.stdout
    assert "session" in top_level.stdout
    assert "shell" in top_level.stdout
    assert "run" in top_level.stdout
    assert ".env" not in top_level.stdout
    assert "validate" in results[2].stdout
    assert "run" in results[2].stdout
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


def test_environment_show_renders_connected_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = ConnectedEnvironment(
        connection_id="connection_" + "7" * 32,
        infrastructure_id="infra_" + "8" * 32,
        scenario_name="connected-scenario",
        objective="Evaluate storage paths",
        identity="start@security-sandbox.iam.gserviceaccount.com",
        target_scope="projects/security-sandbox",
        infrastructure_path=(
            "projects/security-sandbox/buckets/scenario-target"
        ),
        credential_ref="run/default",
        source_kind="impersonate",
        permissions=("storage.objects.create",),
        detection_sources=("sigma",),
    )
    monkeypatch.setattr(
        cli_module,
        "read_connected_environment",
        lambda: environment,
    )

    result = CLI.invoke(app, ["env", "show"])

    assert result.exit_code == 0
    assert "ENVIRONMENT" in result.stdout
    assert "storage.objects.create" in result.stdout
    assert environment.identity in result.stdout


def test_environment_analyse_routes_to_guarded_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, bool, bool]] = []

    def analyse(**values):
        calls.append(
            (
                values["credential_source"],
                values["rebuild_snapshot"],
                values["quiet_trace"],
            )
        )
        return 0

    monkeypatch.setattr(cli_module, "analyse_connected_environment", analyse)

    result = CLI.invoke(
        app,
        ["env", "analyse", "--rebuild-snapshot", "--quiet-trace"],
    )

    assert result.exit_code == 0
    assert calls == [(None, True, True)]


def test_infrastructure_commands_require_dev_mode() -> None:
    result = CLI.invoke(app, ["infra", "list"])

    assert result.exit_code == 2
    assert "DEVELOPMENT MODE REQUIRED" in result.output


def test_dev_infrastructure_create_uses_adc_and_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)
    calls: list[tuple[str, CredentialSourceKind, str, Path]] = []
    record = DevelopmentInfrastructure(
        infrastructure_id="infra_" + "1" * 32,
        path="projects/security-sandbox/buckets/scenario-target",
        project="security-sandbox",
        bucket="scenario-target",
        location="EU",
        starting_principal="start@security-sandbox.iam.gserviceaccount.com",
        created_by="operator@example.test",
    )

    class Service:
        def create(self, scenario, source, *, location, scenario_path):
            calls.append(
                (scenario.name, source.kind, location, scenario_path.parent)
            )
            return record

    monkeypatch.setattr(cli_module, "_infrastructure_service", Service)

    result = CLI.invoke(
        app,
        ["--dev", "infra", "create", "--scenario", str(path)],
    )

    assert result.exit_code == 0
    assert calls == [
        ("test-scenario", CredentialSourceKind.ADC, "EU", tmp_path)
    ]
    assert record.infrastructure_id in result.stdout


def test_dev_infrastructure_destroy_requires_confirmation_and_uses_adc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = DevelopmentInfrastructure(
        infrastructure_id="infra_" + "4" * 32,
        path="projects/security-sandbox/buckets/scenario-target",
        project="security-sandbox",
        bucket="scenario-target",
        location="EU",
        starting_principal="start@security-sandbox.iam.gserviceaccount.com",
        created_by="operator@example.test",
        provisioner="terraform",
        scenario_path="/workspace/scenario.yaml",
    )
    calls: list[tuple[str, CredentialSourceKind]] = []

    class Repository:
        def get(self, infrastructure_id: str):
            assert infrastructure_id == record.infrastructure_id
            return record

    class Service:
        def destroy(self, infrastructure_id, source):
            calls.append((infrastructure_id, source.kind))
            return record.model_copy(
                update={
                    "status": InfrastructureStatus.DESTROYED,
                    "destroyed_at": datetime.now(UTC),
                }
            )

    class ConnectionRepository:
        def optional_active(self):
            return None

    monkeypatch.setattr(cli_module, "_infrastructure_repository", Repository)
    monkeypatch.setattr(cli_module, "_infrastructure_service", Service)
    monkeypatch.setattr(
        cli_module,
        "_connection_repository",
        ConnectionRepository,
    )

    result = CLI.invoke(
        app,
        ["--dev", "infra", "destroy", record.infrastructure_id],
        input="y\n",
    )

    assert result.exit_code == 0
    assert calls == [(record.infrastructure_id, CredentialSourceKind.ADC)]
    assert "DESTROYED" in result.stdout


def test_dev_infrastructure_show_does_not_offer_destroyed_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = DevelopmentInfrastructure(
        infrastructure_id="infra_" + "6" * 32,
        path="projects/security-sandbox/buckets/scenario-target",
        project="security-sandbox",
        bucket="scenario-target",
        location="EU",
        starting_principal="start@security-sandbox.iam.gserviceaccount.com",
        created_by="operator@example.test",
        status=InfrastructureStatus.DESTROYED,
        destroyed_at=datetime.now(UTC),
    )

    class Repository:
        def get(self, infrastructure_id: str):
            assert infrastructure_id == record.infrastructure_id
            return record

    monkeypatch.setattr(cli_module, "_infrastructure_repository", Repository)

    result = CLI.invoke(
        app,
        ["--dev", "infra", "show", record.infrastructure_id],
    )

    assert result.exit_code == 0
    assert "DESTROYED" in result.stdout
    assert "sandbox connect" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "expected_kind", "expected_mode", "expected_id"),
    [
        (
            ["sandbox", "connect"],
            CredentialSourceKind.STDIN,
            False,
            None,
        ),
        (
            ["--dev", "sandbox", "connect", "infra_" + "2" * 32],
            CredentialSourceKind.IMPERSONATE,
            True,
            "infra_" + "2" * 32,
        ),
    ],
)
def test_sandbox_connect_selects_safe_mode_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    expected_kind: CredentialSourceKind,
    expected_mode: bool,
    expected_id: str | None,
) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)
    calls = []
    connection = SandboxConnection(
        connection_id="connection_" + "3" * 32,
        mode=(
            ConnectionMode.DEVELOPMENT
            if expected_mode
            else ConnectionMode.REMOTE
        ),
        infrastructure_id=expected_id,
        infrastructure_path="projects/security-sandbox/buckets/scenario-target",
        scenario_name="test-scenario",
        principal="start@security-sandbox.iam.gserviceaccount.com",
        credential_ref="run/default",
        source_kind=expected_kind,
    )

    class Service:
        def connect(
            self,
            scenario,
            source,
            *,
            development_mode,
            infrastructure_id,
            scenario_path,
        ):
            calls.append(
                (
                    scenario.name,
                    source.kind,
                    source.locator,
                    development_mode,
                    infrastructure_id,
                    scenario_path,
                )
            )
            return connection

    monkeypatch.setattr(cli_module, "_connection_service", Service)

    result = CLI.invoke(
        app,
        [*arguments, "--scenario", str(path)],
    )

    assert result.exit_code == 0
    assert calls[0][0] == "test-scenario"
    assert calls[0][1] == expected_kind
    if expected_mode:
        assert calls[0][2] == "start@security-sandbox.iam.gserviceaccount.com"
    else:
        assert calls[0][2] is None
    assert calls[0][3:5] == (expected_mode, expected_id)
    assert calls[0][5] == path.resolve()
    assert "CONNECTED" in result.stdout


def test_dev_sandbox_connect_infers_scenario_from_infrastructure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario_path = tmp_path / "bound.yaml"
    write_scenario(scenario_path)
    infrastructure_id = "infra_" + "5" * 32
    record = DevelopmentInfrastructure(
        infrastructure_id=infrastructure_id,
        path="projects/security-sandbox/buckets/scenario-target",
        project="security-sandbox",
        bucket="scenario-target",
        location="EU",
        starting_principal="start@security-sandbox.iam.gserviceaccount.com",
        created_by="operator@example.test",
        provisioner="terraform",
        scenario_path=str(scenario_path),
    )
    calls: list[Path] = []

    class Repository:
        def get(self, requested_id: str):
            assert requested_id == infrastructure_id
            return record

    class Service:
        def connect(self, scenario, source, **values):
            del scenario, source
            calls.append(values["scenario_path"])
            return SandboxConnection(
                connection_id="connection_" + "6" * 32,
                mode=ConnectionMode.DEVELOPMENT,
                infrastructure_id=infrastructure_id,
                infrastructure_path=record.path,
                scenario_name="test-scenario",
                scenario_path=str(scenario_path),
                principal=record.starting_principal,
                credential_ref="run/default",
                source_kind=CredentialSourceKind.IMPERSONATE,
            )

    monkeypatch.setattr(cli_module, "_infrastructure_repository", Repository)
    monkeypatch.setattr(cli_module, "_connection_service", Service)

    result = CLI.invoke(
        app,
        ["--dev", "sandbox", "connect", infrastructure_id],
    )

    assert result.exit_code == 0
    assert calls == [scenario_path.resolve()]


def test_session_routes_render_persisted_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAM_PLANNER_RUNTIME", str(tmp_path))
    repository = ShellSessionRepository(tmp_path / "shell" / "sessions")
    session = repository.record(
        repository.create(),
        operation="corpus status",
        exit_code=0,
    )

    listed = CLI.invoke(app, ["session", "list"])
    shown = CLI.invoke(app, ["session", "show", session.session_id])

    assert listed.exit_code == 0
    assert session.session_id in listed.stdout
    assert shown.exit_code == 0
    assert "corpus status" in shown.stdout


def test_session_show_rejects_invalid_identifier() -> None:
    result = CLI.invoke(app, ["session", "show", "../current"])

    assert result.exit_code == 2
    assert "INPUT ERROR" in result.output


def test_shell_command_creates_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAM_PLANNER_RUNTIME", str(tmp_path))

    result = CLI.invoke(app, ["shell"], input="/exit\n")

    sessions = ShellSessionRepository(tmp_path / "shell" / "sessions").list()
    assert result.exit_code == 0
    assert "NEW SESSION" in result.stdout
    assert "saved" in result.stdout
    assert len(sessions) == 1
    assert sessions[0].status.value == "closed"


def test_no_arguments_opens_shell_only_for_interactive_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []
    monkeypatch.setattr(cli_module, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "_run_shell",
        lambda session_id=None, *, dev_mode=False: calls.append(session_id),
    )

    result = CLI.invoke(app, [])

    assert result.exit_code == 0
    assert calls == [None]


def test_dev_option_opens_development_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(cli_module, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "_run_shell",
        lambda session_id=None, *, dev_mode=False: calls.append(dev_mode),
    )

    result = CLI.invoke(app, ["--dev"])

    assert result.exit_code == 0
    assert calls == [True]


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


def test_sandbox_connect_rejects_positional_token_without_echoing_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scenario.yaml"
    write_scenario(path)

    result = CLI.invoke(
        app,
        ["sandbox", "connect", TEST_SECRET, "--scenario", str(path)],
    )

    assert result.exit_code == 2
    assert "only with the --dev option" in result.output
    assert TEST_SECRET not in result.output


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
    source = parse_choice("s1", candidates)
    assert source.decision is None
    assert source.inspection == "source"
    assert source.candidate_id == "candidate"
    pending_input = parse_choice("i1", candidates)
    assert pending_input.decision is None
    assert pending_input.inspection == "input"
    question = parse_choice("where did this tool come from?", candidates)
    assert question.decision is None
    assert question.question == "where did this tool come from?"
    explicit_message = parse_choice('message "tell me a story"', candidates)
    assert explicit_message.decision is None
    assert explicit_message.question == "tell me a story"
    with pytest.raises(ValueError):
        parse_choice("", candidates)
    with pytest.raises(ValueError, match="message requires text"):
        parse_choice("message", candidates)


def test_terminal_review_displays_candidate_evidence() -> None:
    stream = StringIO()
    displayed = candidate().model_copy(
        update={
            "technique_title": "Test technique",
            "expected_capabilities": ("test-capability",),
            "validation": candidate().validation.model_copy(
                update={
                    "covered_permissions": ("covered.permission",),
                    "uncovered_permissions": ("test.permission",),
                    "matching_detection_ids": ("detection-id",),
                }
            ),
        }
    )
    terminal = TerminalUI(
        Console(file=stream, color_system=None, highlight=False, width=100)
    )

    terminal.candidates((displayed,))

    rendered = stream.getvalue()
    assert "SELECTION REQUIRED" in rendered
    assert "Test technique" in rendered
    assert "test-capability" in rendered
    assert "covered.permission" in rendered
    assert "test.permission" in rendered
    assert "detection-id" in rendered
    assert "/execute" not in rendered


def test_terminal_action_review_distinguishes_execution_approval() -> None:
    stream = StringIO()
    terminal = TerminalUI(
        Console(file=stream, color_system=None, highlight=False, width=100)
    )

    content = "print('approved')\n"
    action_card = candidate().model_copy(
        update={
            "action_command": ActionCommand(
                action_id=candidate().proposal.action_id,
                approval_id="approval_" + "1" * 32,
                display="/usr/local/bin/python /workspace/action.py",
                prepared_by="model",
                input_summary=(
                    "The approval envelope will be assembled in memory after "
                    "approval and passed on stdin; no local payload file exists."
                ),
                side_effects=(
                    "Create gs://scenario-target/actions/approval_"
                    + "1" * 32
                    + ".json",
                ),
                tool_source="action.py",
                tool_installation=(
                    "Authored by fixture-model and written after explicit approval."
                ),
                artifact=ActionArtifact(
                    content=content,
                    digest=(
                        "sha256:"
                        + hashlib.sha256(content.encode("utf-8")).hexdigest()
                    ),
                    source_model="fixture-model",
                    rationale="Fixture action.",
                    written=True,
                    workspace_path="/runtime/action.py",
                ),
            )
        }
    )

    terminal.candidates(
        (action_card,),
        review_stage=ReviewStage.ACTION_EXECUTION,
    )

    rendered = stream.getvalue()
    assert "ACTION COMMAND REVIEW" in rendered
    assert "EXECUTION APPROVAL REQUIRED" in rendered
    assert "/usr/local/bin/python" in rendered
    assert "/workspace/action.py" in rendered
    assert "<approval_id>" not in rendered
    assert "Model-authored file + fixed harness" in rendered
    assert "command not run" in rendered
    assert "assembled in memory after approval" in rendered
    assert "no local payload file exists" in rendered
    assert "Will change" in rendered
    assert "gs://scenario-target/actions/approval_" in rendered
    assert "Revalidate" in rendered
    assert "record one-time approval" in rendered
    assert "/runtime/action.py" in rendered
    assert "Authored by fixture-model" in rendered
    assert "Reason" in rendered
    assert "Covered" not in rendered
    assert "Validation" not in rendered


def test_terminal_renders_approved_command_output() -> None:
    stream = StringIO()
    terminal = TerminalUI(
        Console(file=stream, color_system=None, highlight=False, width=100)
    )
    observation = ExecutionObservation(
        execution_id="gcp-approval",
        engagement_id="engagement",
        action_id="technique:test",
        identity="identity",
        target="projects/project/buckets/scenario-target",
        success=True,
        api_response_summary=(
            "created gs://scenario-target/actions/approval.json"
        ),
        command_stdout=(
            '{"bucket":"scenario-target","name":"actions/approval.json"}\n'
        ),
        explanation="The upload command created the approved object.",
        next_steps=(
            "Retrieve and inspect the created object.",
            "Run env show to inspect environment state.",
        ),
        discovered_resources=(
            "gs://scenario-target/actions/approval.json",
        ),
    )

    command = ActionCommand(
        action_id="technique:test",
        approval_id="approval_" + "1" * 32,
        display=(
            "/usr/local/bin/python /opt/verisentinel/gcs_upload.py "
            "--bucket scenario-target --object "
            f"actions/approval_{'1' * 32}.json"
        ),
    )

    terminal.command_output(
        observation,
        command=command,
        state_version="sha256:state",
        completion_evidence=(
            "FLAG{gcs-action-delivery:approval_" + "1" * 32 + "}"
        ),
    )

    rendered = stream.getvalue()
    assert "COMMAND OUTPUT  SUCCESS" in rendered
    assert '"bucket":"scenario-target"' in rendered
    assert "The upload command created the approved object." in rendered
    assert "1. Retrieve and inspect the created object." in rendered
    assert "/usr/local/bin/python" in rendered
    assert "gcp-approval" in rendered
    assert "sha256:state" in rendered
    assert "FLAG{gcs-action-delivery:approval_" in rendered
    assert "OBJECTIVE VERIFIED" in rendered
    assert rendered.index("Stdout") < rendered.index("Explanation")
    assert rendered.index("Explanation") < rendered.index("Next steps")
    assert "<approval_id>" not in rendered


def test_terminal_renders_registered_source_input_and_model_turn() -> None:
    stream = StringIO()
    terminal = TerminalUI(
        Console(file=stream, color_system=None, highlight=False, width=100)
    )

    terminal.command_source(
        reference="execution/gateway/gcs_upload.py",
        installation="Copied into the gateway image during sandbox build.",
        digest="a" * 64,
        content='print("tool source")\n',
    )
    terminal.command_input('{"approval_id":"approval_123"}')
    terminal.agent_message("This is a preinstalled tool; nothing has executed.")

    rendered = stream.getvalue()
    assert "REGISTERED TOOL SOURCE" in rendered
    assert "execution/gateway/gcs_upload.py" in rendered
    assert 'print("tool source")' in rendered
    assert "PENDING COMMAND INPUT" in rendered
    assert '"approval_id":"approval_123"' in rendered
    assert "Credential material is injected only after approval" in rendered
    assert "MODEL  NO ACTION EXECUTED" in rendered
