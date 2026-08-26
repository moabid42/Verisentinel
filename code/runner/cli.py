"""Unified Typer command-line interface for Verisentinel."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from core.errors import AuthorizationError, PlannerError
from execution.credentials import CredentialSourceError, parse_credential_source
from runner.application import (
    CorpusStatus,
    PlannerConfigurationError,
    PlannerRunRequest,
    build_corpus,
    inspect_authentication,
    inspect_sandbox,
    read_corpus_status,
    run_planner,
    validate_scenario,
)
from runner.scenario import ScenarioError

_OUTPUT_LIMIT = 512

_APP_SETTINGS = {
    "add_completion": False,
    "context_settings": {"help_option_names": ["-h", "--help"]},
    "no_args_is_help": True,
    "pretty_exceptions_enable": False,
    "rich_markup_mode": None,
}

app = typer.Typer(
    name="verisentinel",
    help="Validate coverage plans and run explicitly approved simulations.",
    **_APP_SETTINGS,
)
scenario_app = typer.Typer(help="Validate planner scenario files.", **_APP_SETTINGS)
auth_app = typer.Typer(help="Inspect credential source metadata.", **_APP_SETTINGS)
corpus_app = typer.Typer(help="Build and inspect the coverage corpus.", **_APP_SETTINGS)
sandbox_app = typer.Typer(help="Inspect controlled-execution readiness.", **_APP_SETTINGS)

app.add_typer(scenario_app, name="scenario")
app.add_typer(auth_app, name="auth")
app.add_typer(corpus_app, name="corpus")
app.add_typer(sandbox_app, name="sandbox")


@scenario_app.command("validate")
def scenario_validate(
    path: Annotated[Path, typer.Argument(help="Planner scenario YAML path.")],
) -> None:
    """Validate a scenario without starting a planner run."""
    _invoke(
        lambda: validate_scenario(path),
        input_errors=(ScenarioError,),
    )
    typer.echo("Scenario is valid.")


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
    typer.echo(f"Source kind: {inspection.source_kind.value}")
    typer.echo(f"Principal: {_bounded(inspection.principal)}")
    typer.echo(f"Expires at: {inspection.expires_at.isoformat()}")


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
    typer.echo(f"Provider: {status.provider}")
    typer.echo(f"Status: {'available' if status.available else 'unavailable'}")
    typer.echo(f"Detail: {_bounded(status.detail)}")


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
        typer.echo(
            "Interrupted. No implicit approval or execution was performed.",
            err=True,
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
        typer.echo(
            "Interrupted. No implicit approval or execution was performed.",
            err=True,
        )
        raise typer.Exit(130) from None
    except input_errors as error:
        message = input_message or str(error)
        typer.echo(f"Input error: {_bounded(message)}", err=True)
        raise typer.Exit(2) from None
    except (PlannerError, OSError, RuntimeError) as error:
        typer.echo(f"Application failure: {_bounded(str(error))}", err=True)
        raise typer.Exit(1) from None
    except Exception as error:
        typer.echo(f"Application failure: {type(error).__name__}", err=True)
        raise typer.Exit(1) from None


def _render_corpus(status: CorpusStatus) -> None:
    if not status.available:
        typer.echo("Status: not built")
        return
    typer.echo("Status: available")
    typer.echo(f"Matrix version: {_bounded(status.matrix_version or '')}")
    typer.echo(f"Permissions: {status.permission_count}")
    typer.echo(f"Detections: {status.detection_count}")
    typer.echo(f"Techniques: {status.technique_count}")


def _bounded(value: str) -> str:
    if len(value) <= _OUTPUT_LIMIT:
        return value
    return value[: _OUTPUT_LIMIT - 3] + "..."
