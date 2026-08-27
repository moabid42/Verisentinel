"""Tests for the session-oriented interactive terminal shell."""

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from runner.session import ShellSessionRepository, ShellSessionStatus
from runner.shell import InteractiveShell, normalize_shell_command
from runner.terminal import TerminalUI


class ScriptedReader:
    """Return a fixed sequence of interactive commands."""

    def __init__(self, *commands: str) -> None:
        self.commands = iter(commands)

    def __call__(self, prompt: str) -> str:
        del prompt
        try:
            return next(self.commands)
        except StopIteration:
            raise EOFError from None


def terminal(stream: StringIO) -> TerminalUI:
    return TerminalUI(
        Console(file=stream, color_system=None, highlight=False, width=100)
    )


def test_shell_dispatches_natural_commands_and_saves_session(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    stream = StringIO()
    repository = ShellSessionRepository(tmp_path)
    shell = InteractiveShell(
        repository=repository,
        executor=lambda arguments: calls.append(arguments) or 0,
        terminal=terminal(stream),
        reader=ScriptedReader(
            "build sandbox",
            "run scenario scenario.yaml --credential-source env:SENSITIVE_NAME",
            "auth SENSITIVE_SUBCOMMAND",
            "/history",
            "/exit",
        ),
    )

    exit_code = shell.run()

    assert exit_code == 0
    assert calls == [
        ["sandbox", "build"],
        [
            "scenario",
            "run",
            "scenario.yaml",
            "--credential-source",
            "env:SENSITIVE_NAME",
        ],
        ["auth", "SENSITIVE_SUBCOMMAND"],
    ]
    session = repository.list()[0]
    assert session.status == ShellSessionStatus.CLOSED
    assert [event.operation for event in session.events] == [
        "sandbox build",
        "scenario run",
        "auth",
    ]
    assert "SENSITIVE_NAME" not in session.model_dump_json()
    assert "SENSITIVE_SUBCOMMAND" not in session.model_dump_json()
    assert "scenario run" in stream.getvalue()


def test_shell_rejects_system_commands_without_dispatch(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    stream = StringIO()
    shell = InteractiveShell(
        repository=ShellSessionRepository(tmp_path),
        executor=lambda arguments: calls.append(arguments) or 0,
        terminal=terminal(stream),
        reader=ScriptedReader("rm -rf /tmp/example", "/exit"),
    )

    shell.run()

    assert not calls
    assert "Verisentinel commands only" in stream.getvalue()


def test_shell_resumes_existing_session(tmp_path: Path) -> None:
    repository = ShellSessionRepository(tmp_path)
    existing = repository.close(repository.create())
    stream = StringIO()
    shell = InteractiveShell(
        repository=repository,
        executor=lambda arguments: 0,
        terminal=terminal(stream),
        reader=ScriptedReader("/exit"),
    )

    shell.run(existing.session_id)

    assert "RESUMED" in stream.getvalue()
    assert repository.get(existing.session_id).status == ShellSessionStatus.CLOSED


def test_shell_recovers_from_unmatched_quote(tmp_path: Path) -> None:
    stream = StringIO()
    shell = InteractiveShell(
        repository=ShellSessionRepository(tmp_path),
        executor=lambda arguments: 0,
        terminal=terminal(stream),
        reader=ScriptedReader("scenario validate 'missing", "/exit"),
    )

    shell.run()

    assert "unmatched quote" in stream.getvalue()


def test_shell_marks_session_interrupted_after_unexpected_failure(
    tmp_path: Path,
) -> None:
    def fail(arguments: list[str]) -> int:
        del arguments
        raise RuntimeError("failure")

    repository = ShellSessionRepository(tmp_path)
    shell = InteractiveShell(
        repository=repository,
        executor=fail,
        terminal=terminal(StringIO()),
        reader=ScriptedReader("corpus status"),
    )

    with pytest.raises(RuntimeError, match="failure"):
        shell.run()

    assert repository.list()[0].status == ShellSessionStatus.INTERRUPTED


def test_shell_normalizes_optional_program_name() -> None:
    assert normalize_shell_command("verisentinel corpus status") == [
        "corpus",
        "status",
    ]


def test_infrastructure_commands_require_development_mode(tmp_path: Path) -> None:
    regular_calls: list[list[str]] = []
    development_calls: list[list[str]] = []
    regular = InteractiveShell(
        repository=ShellSessionRepository(tmp_path / "regular"),
        executor=lambda arguments: regular_calls.append(arguments) or 0,
        terminal=terminal(StringIO()),
        reader=ScriptedReader("infra list", "/exit"),
    )
    development = InteractiveShell(
        repository=ShellSessionRepository(tmp_path / "development"),
        executor=lambda arguments: development_calls.append(arguments) or 0,
        terminal=terminal(StringIO()),
        reader=ScriptedReader("infra list", "/exit"),
        dev_mode=True,
    )

    regular.run()
    development.run()

    assert not regular_calls
    assert development_calls == [["infra", "list"]]
