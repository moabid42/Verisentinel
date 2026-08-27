"""Interactive, session-oriented command shell for Verisentinel."""

from __future__ import annotations

import shlex
from collections.abc import Callable

from runner.session import ShellSession, ShellSessionRepository
from runner.terminal import TerminalUI

_TOP_LEVEL_COMMANDS = frozenset(
    {"auth", "corpus", "run", "sandbox", "scenario", "session"}
)
_KNOWN_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "auth": frozenset({"inspect"}),
    "corpus": frozenset({"build", "status"}),
    "sandbox": frozenset({"build", "doctor"}),
    "scenario": frozenset({"run", "validate"}),
    "session": frozenset({"list", "resume", "show"}),
}
_COMMAND_ALIASES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("build", "corpus"): ("corpus", "build"),
    ("build", "sandbox"): ("sandbox", "build"),
    ("check", "sandbox"): ("sandbox", "doctor"),
    ("run", "scenario"): ("scenario", "run"),
    ("validate", "scenario"): ("scenario", "validate"),
}

ShellReader = Callable[[str], str]
CommandExecutor = Callable[[list[str]], int]


class InteractiveShell:
    """Run known Verisentinel commands inside one persistent terminal session."""

    def __init__(
        self,
        *,
        repository: ShellSessionRepository,
        executor: CommandExecutor,
        terminal: TerminalUI | None = None,
        reader: ShellReader | None = None,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.terminal = terminal or TerminalUI()
        self.reader = reader or self.terminal.read_shell_command

    def run(self, resume_session_id: str | None = None) -> int:
        """Start or resume a session and process input until explicit exit or EOF."""
        session = (
            self.repository.resume(resume_session_id)
            if resume_session_id
            else self.repository.create()
        )
        self.terminal.shell_started(session, resumed=resume_session_id is not None)
        while True:
            try:
                raw = self.reader(self.terminal.shell_prompt(session))
            except KeyboardInterrupt:
                self.terminal.shell_interrupted()
                continue
            except EOFError:
                session = self.repository.close(session)
                self.terminal.shell_closed(session)
                return 0
            except Exception:
                self.repository.close(session, interrupted=True)
                raise

            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("/") or stripped.lower() in {
                "?",
                "--help",
                "exit",
                "help",
                "quit",
            }:
                session, should_exit = self._slash_command(stripped, session)
                if should_exit:
                    return 0
                continue

            try:
                arguments = normalize_shell_command(stripped)
            except ValueError as error:
                self.terminal.shell_error(str(error))
                continue
            if arguments[0] not in _TOP_LEVEL_COMMANDS:
                self.terminal.shell_error(
                    "unknown command; this prompt accepts Verisentinel commands only"
                )
                continue

            try:
                exit_code = self.executor(arguments)
            except KeyboardInterrupt:
                exit_code = 130
                self.terminal.shell_interrupted()
            except Exception:
                self.repository.close(session, interrupted=True)
                raise
            session = self.repository.record(
                session,
                operation=_operation_name(arguments),
                exit_code=exit_code,
            )
            if exit_code:
                self.terminal.shell_command_failed(exit_code)

    def _slash_command(
        self,
        raw: str,
        session: ShellSession,
    ) -> tuple[ShellSession, bool]:
        command = raw.lower().removeprefix("/")
        if command in {"exit", "quit"}:
            closed = self.repository.close(session)
            self.terminal.shell_closed(closed)
            return closed, True
        if command in {"help", "?", "--help"}:
            self.terminal.shell_help()
            return session, False
        if command in {"status", "history"}:
            self.terminal.session(session)
            return session, False
        if command == "sessions":
            self.terminal.sessions(self.repository.list())
            return session, False
        if command == "clear":
            self.terminal.clear()
            return session, False
        if command == "new":
            self.repository.close(session)
            created = self.repository.create()
            self.terminal.shell_started(created, resumed=False)
            return created, False
        self.terminal.shell_error("unknown slash command; enter /help to see available actions")
        return session, False


def normalize_shell_command(raw: str) -> list[str]:
    """Parse a command line and map natural command forms to canonical routes."""
    try:
        arguments = shlex.split(raw)
    except ValueError as error:
        raise ValueError("command contains an unmatched quote") from error
    if arguments and arguments[0].lower() == "verisentinel":
        arguments = arguments[1:]
    if not arguments:
        raise ValueError("enter a command or /help")

    lowered = tuple(argument.lower() for argument in arguments[:2])
    replacement = _COMMAND_ALIASES.get(lowered)
    if replacement is not None:
        arguments = [*replacement, *arguments[2:]]
    return arguments


def _operation_name(arguments: list[str]) -> str:
    command = arguments[0].lower()
    subcommand = arguments[1].lower() if len(arguments) > 1 else ""
    if subcommand in _KNOWN_SUBCOMMANDS.get(command, frozenset()):
        return f"{command} {subcommand}"
    return command
