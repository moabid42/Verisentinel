"""Unified Typer command-line interface for Verisentinel."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from core.config import Paths
from core.errors import AuthorizationError, NotFoundError, PlannerError
from execution.credentials import CredentialSourceError, parse_credential_source
from runner.application import (
    CorpusStatus,
    PlannerConfigurationError,
    PlannerRunRequest,
    build_corpus,
    build_sandbox,
    inspect_authentication,
    inspect_sandbox,
    read_corpus_status,
    run_planner,
    validate_scenario,
)
from runner.connection import (
    SandboxConnectionRepository,
    SandboxConnectionService,
)
from runner.infrastructure import InfrastructureRepository, InfrastructureService
from runner.scenario import ScenarioError, load_scenario
from runner.session import ShellSessionRepository
from runner.shell import InteractiveShell
from runner.terminal import TerminalUI

_OUTPUT_LIMIT = 512

_APP_SETTINGS = {
    "add_completion": False,
    "context_settings": {"help_option_names": ["-h", "--help"]},
    "pretty_exceptions_enable": False,
    "rich_markup_mode": "rich",
}

app = typer.Typer(
    name="verisentinel",
    help="Authorization analysis, coverage validation, and controlled simulation.",
    no_args_is_help=False,
    **_APP_SETTINGS,
)
scenario_app = typer.Typer(
    help="Validate and run scenario files.", no_args_is_help=True, **_APP_SETTINGS
)
auth_app = typer.Typer(
    help="Inspect credential source metadata.", no_args_is_help=True, **_APP_SETTINGS
)
corpus_app = typer.Typer(
    help="Build and inspect the coverage corpus.", no_args_is_help=True, **_APP_SETTINGS
)
sandbox_app = typer.Typer(
    help="Build and verify controlled execution.", no_args_is_help=True, **_APP_SETTINGS
)
session_app = typer.Typer(
    help="Inspect interactive terminal sessions.",
    no_args_is_help=True,
    **_APP_SETTINGS,
)
infra_app = typer.Typer(
    help="Provision disposable development infrastructure.",
    no_args_is_help=True,
    **_APP_SETTINGS,
)

app.add_typer(scenario_app, name="scenario")
app.add_typer(auth_app, name="auth")
app.add_typer(corpus_app, name="corpus")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(session_app, name="session")
app.add_typer(infra_app, name="infra")


@app.callback(invoke_without_command=True)
def launchpad(
    context: typer.Context,
    dev: Annotated[
        bool,
        typer.Option(
            "--dev",
            help="Enable development infrastructure and live-delivery controls.",
        ),
    ] = False,
) -> None:
    """Open the terminal command launchpad."""
    context.ensure_object(dict)
    context.obj["dev"] = dev
    if context.invoked_subcommand is None:
        if _interactive_terminal():
            _run_shell(dev_mode=dev)
        else:
            TerminalUI().home(dev_mode=dev)


@app.command("shell")
def shell_command(
    context: typer.Context,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            metavar="SESSION_ID",
            help="Resume an existing terminal session.",
        ),
    ] = None,
) -> None:
    """Open the persistent interactive terminal shell."""
    _run_shell(resume, dev_mode=_dev_mode(context))


@scenario_app.command("validate")
def scenario_validate(
    path: Annotated[Path, typer.Argument(help="Planner scenario YAML path.")],
) -> None:
    """Validate a scenario without starting a planner run."""
    scenario = _invoke(
        lambda: validate_scenario(path),
        input_errors=(ScenarioError,),
    )
    TerminalUI().success(
        "SCENARIO VALID",
        f"{_bounded(scenario.name)} · {_bounded(scenario.target_scope)}",
    )


@scenario_app.command("run")
def scenario_run(
    path: Annotated[Path, typer.Argument(help="Planner scenario YAML path.")],
    credential_source: Annotated[
        str,
        typer.Option(
            "--credential-source",
            metavar="SOURCE",
            help=(
                "Credential source: stdin, file:<path>, env:<name>, adc, or "
                "impersonate:<principal>."
            ),
        ),
    ],
    rebuild_snapshot: Annotated[
        bool,
        typer.Option(
            "--rebuild-snapshot",
            help="Rebuild the configured corpus before starting.",
        ),
    ] = False,
    quiet_trace: Annotated[
        bool,
        typer.Option(
            "--quiet-trace",
            help="Do not echo trace event names to standard error.",
        ),
    ] = False,
) -> None:
    """Run a scenario through explicit terminal review."""
    _run_scenario(path, credential_source, rebuild_snapshot, quiet_trace)


@auth_app.command("inspect")
def auth_inspect(
    credential_source: Annotated[
        str,
        typer.Option(
            "--credential-source",
            metavar="SOURCE",
            help=(
                "Credential source: stdin, file:<path>, env:<name>, adc, or "
                "impersonate:<principal>."
            ),
        ),
    ],
) -> None:
    """Resolve a source briefly and print only verified public metadata."""
    source = _invoke(
        lambda: parse_credential_source(credential_source),
        input_errors=(CredentialSourceError,),
        input_message="Credential source is unsupported or invalid.",
    )
    inspection = _invoke(
        lambda: inspect_authentication(source),
        input_errors=(AuthorizationError,),
    )
    TerminalUI().authentication(
        source_kind=inspection.source_kind.value,
        principal=_bounded(inspection.principal),
        expires_at=inspection.expires_at.isoformat(),
    )


@corpus_app.command("build")
def corpus_build() -> None:
    """Build the default coverage corpus."""
    status = _invoke(build_corpus)
    _render_corpus(status)


@corpus_app.command("status")
def corpus_status() -> None:
    """Show the current coverage corpus without rebuilding it."""
    status = _invoke(read_corpus_status)
    _render_corpus(status)


@sandbox_app.command("doctor")
def sandbox_doctor() -> None:
    """Report local execution capsule readiness."""
    status = _invoke(inspect_sandbox)
    TerminalUI().sandbox_doctor(status)


@sandbox_app.command("build")
def sandbox_build() -> None:
    """Build the locked capsule image and prepare its private network."""
    report = _invoke(build_sandbox)
    TerminalUI().sandbox_build(report)


@sandbox_app.command("connect")
def sandbox_connect(
    context: typer.Context,
    infrastructure_id: Annotated[
        str | None,
        typer.Argument(help="Development infrastructure ID; accepted only with --dev."),
    ] = None,
    scenario_path: Annotated[
        Path,
        typer.Option(
            "--scenario",
            metavar="PATH",
            help="Scenario that declares the remote path and starting user.",
        ),
    ] = Path("scenario.yaml"),
    credential_source: Annotated[
        str | None,
        typer.Option(
            "--credential-source",
            metavar="SOURCE",
            help=(
                "Credential source. Defaults to scenario-user impersonation in "
                "development mode and a hidden stdin token otherwise."
            ),
        ),
    ] = None,
) -> None:
    """Verify a scenario user and activate its remote infrastructure."""
    development_mode = _dev_mode(context)
    scenario = _invoke(
        lambda: load_scenario(scenario_path.expanduser().resolve()),
        input_errors=(ScenarioError,),
    )
    source_value = credential_source or (
        f"impersonate:{scenario.starting_service_account.identity}" if development_mode else "stdin"
    )
    source = _invoke(
        lambda: parse_credential_source(source_value),
        input_errors=(CredentialSourceError,),
        input_message="Credential source is unsupported or invalid.",
    )
    connection = _invoke(
        lambda: _connection_service().connect(
            scenario,
            source,
            development_mode=development_mode,
            infrastructure_id=infrastructure_id,
        ),
        input_errors=(ValueError,),
    )
    TerminalUI().sandbox_connection(connection)


@sandbox_app.command("status")
def sandbox_status() -> None:
    """Show the active non-sensitive infrastructure connection."""
    connection = _invoke(_connection_repository().optional_active)
    TerminalUI().sandbox_connection_status(connection)


@infra_app.command("create")
def infrastructure_create(
    context: typer.Context,
    scenario_path: Annotated[
        Path,
        typer.Option(
            "--scenario",
            metavar="PATH",
            help="Scenario that declares the bucket path and starting user.",
        ),
    ] = Path("scenario.yaml"),
    location: Annotated[
        str,
        typer.Option(
            "--location",
            metavar="REGION",
            help="Cloud Storage location for the disposable target.",
        ),
    ] = "EU",
) -> None:
    """Create a private scenario target using gcloud ADC."""
    _require_dev(context)
    scenario = _invoke(
        lambda: load_scenario(scenario_path.expanduser().resolve()),
        input_errors=(ScenarioError,),
    )
    source = parse_credential_source("adc")
    infrastructure = _invoke(
        lambda: _infrastructure_service().create(
            scenario,
            source,
            location=location,
        )
    )
    TerminalUI().infrastructure_created(infrastructure)


@infra_app.command("list")
def infrastructure_list(context: typer.Context) -> None:
    """List development infrastructure known to this workspace."""
    _require_dev(context)
    TerminalUI().infrastructure_list(_invoke(_infrastructure_repository().list))


@infra_app.command("show")
def infrastructure_show(
    context: typer.Context,
    infrastructure_id: Annotated[
        str,
        typer.Argument(help="Opaque development infrastructure ID."),
    ],
) -> None:
    """Show one non-sensitive development infrastructure record."""
    _require_dev(context)
    infrastructure = _invoke(
        lambda: _infrastructure_repository().get(infrastructure_id),
        input_errors=(NotFoundError, ValueError),
    )
    TerminalUI().infrastructure_created(infrastructure)


@session_app.command("list")
def session_list() -> None:
    """List persisted interactive terminal sessions."""
    sessions = _invoke(_session_repository().list)
    TerminalUI().sessions(sessions)


@session_app.command("show")
def session_show(
    session_id: Annotated[str, typer.Argument(help="Opaque terminal session ID.")],
) -> None:
    """Show sanitized history for one terminal session."""
    session = _invoke(
        lambda: _session_repository().get(session_id),
        input_errors=(NotFoundError, ValueError),
    )
    TerminalUI().session(session)


@session_app.command("resume")
def session_resume(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Opaque terminal session ID.")],
) -> None:
    """Resume an existing interactive terminal session."""
    _run_shell(session_id, dev_mode=_dev_mode(context))


@app.command("run")
def run_command(
    scenario: Annotated[
        Path,
        typer.Option(
            "--scenario",
            metavar="PATH",
            help="Planner scenario YAML path.",
        ),
    ],
    credential_source: Annotated[
        str,
        typer.Option(
            "--credential-source",
            metavar="SOURCE",
            help=(
                "Credential source: stdin, file:<path>, env:<name>, adc, or "
                "impersonate:<principal>."
            ),
        ),
    ],
    rebuild_snapshot: Annotated[
        bool,
        typer.Option(
            "--rebuild-snapshot",
            help="Rebuild the configured corpus before starting.",
        ),
    ] = False,
    quiet_trace: Annotated[
        bool,
        typer.Option(
            "--quiet-trace",
            help="Do not echo trace event names to standard error.",
        ),
    ] = False,
) -> None:
    """Run the direct human-gated planner with the simulator default."""
    _run_scenario(scenario, credential_source, rebuild_snapshot, quiet_trace)


def _run_scenario(
    scenario: Path,
    credential_source: str,
    rebuild_snapshot: bool,
    quiet_trace: bool,
) -> None:
    source = _invoke(
        lambda: parse_credential_source(credential_source),
        input_errors=(CredentialSourceError,),
        input_message="Credential source is unsupported or invalid.",
    )
    exit_code = _invoke(
        lambda: run_planner(
            PlannerRunRequest(
                scenario_path=scenario,
                credential_source=source,
                rebuild_snapshot=rebuild_snapshot,
                quiet_trace=quiet_trace,
            )
        ),
        input_errors=(ScenarioError, PlannerConfigurationError),
    )
    if exit_code:
        raise typer.Exit(exit_code)


def main(arguments: list[str] | None = None) -> int:
    """Invoke the Typer application and return a process exit code."""
    try:
        app(
            args=arguments,
            prog_name="verisentinel",
        )
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    except KeyboardInterrupt:
        TerminalUI.errors().error(
            "INTERRUPTED",
            "No implicit approval or execution was performed.",
        )
        return 130
    return 0


def _invoke[Result](
    operation: Callable[[], Result],
    *,
    input_errors: tuple[type[Exception], ...] = (),
    input_message: str | None = None,
) -> Result:
    try:
        return operation()
    except KeyboardInterrupt:
        TerminalUI.errors().error(
            "INTERRUPTED",
            "No implicit approval or execution was performed.",
        )
        raise typer.Exit(130) from None
    except input_errors as error:
        message = input_message or str(error)
        TerminalUI.errors().error("INPUT ERROR", _bounded(message))
        raise typer.Exit(2) from None
    except (PlannerError, OSError, RuntimeError) as error:
        TerminalUI.errors().error("APPLICATION FAILURE", _bounded(str(error)))
        raise typer.Exit(1) from None
    except Exception as error:
        TerminalUI.errors().error("APPLICATION FAILURE", type(error).__name__)
        raise typer.Exit(1) from None


def _render_corpus(status: CorpusStatus) -> None:
    TerminalUI().corpus(
        available=status.available,
        matrix_version=_bounded(status.matrix_version or ""),
        permission_count=status.permission_count,
        detection_count=status.detection_count,
        technique_count=status.technique_count,
    )


def _session_repository() -> ShellSessionRepository:
    return ShellSessionRepository(Paths().runtime / "shell" / "sessions")


def _infrastructure_repository() -> InfrastructureRepository:
    return InfrastructureRepository(Paths().runtime / "infrastructure")


def _infrastructure_service() -> InfrastructureService:
    return InfrastructureService(_infrastructure_repository())


def _connection_repository() -> SandboxConnectionRepository:
    return SandboxConnectionRepository(Paths().runtime / "sandbox" / "connection")


def _connection_service() -> SandboxConnectionService:
    return SandboxConnectionService(
        _connection_repository(),
        infrastructure=_infrastructure_repository(),
    )


def _run_shell(
    resume_session_id: str | None = None,
    *,
    dev_mode: bool = False,
) -> None:
    executor: Callable[[list[str]], int] = (
        (lambda arguments: main(["--dev", *arguments])) if dev_mode else main
    )
    shell = InteractiveShell(
        repository=_session_repository(),
        executor=executor,
        dev_mode=dev_mode,
    )
    _invoke(
        lambda: shell.run(resume_session_id),
        input_errors=(NotFoundError, ValueError),
    )


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _dev_mode(context: typer.Context) -> bool:
    return bool(context.find_root().params.get("dev", False))


def _require_dev(context: typer.Context) -> None:
    if not _dev_mode(context):
        TerminalUI.errors().error(
            "DEVELOPMENT MODE REQUIRED",
            "Restart with python3 run.py --dev before managing infrastructure.",
        )
        raise typer.Exit(2)


def _bounded(value: str) -> str:
    if len(value) <= _OUTPUT_LIMIT:
        return value
    return value[: _OUTPUT_LIMIT - 3] + "..."
