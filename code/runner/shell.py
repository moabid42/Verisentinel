"""Interactive, session-oriented command shell for Verisentinel."""

from __future__ import annotations

import shlex
from collections.abc import Callable

from runner.connection import SandboxConnection
from runner.session import ShellSession, ShellSessionRepository
from runner.terminal import TerminalUI

_TOP_LEVEL_COMMANDS = frozenset(
    {"auth", "corpus", "env", "run", "sandbox", "scenario", "session"}
)
_KNOWN_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "auth": frozenset({"inspect"}),
    "corpus": frozenset({"build", "status"}),
    "env": frozenset({"analyse", "analyze", "show"}),
    "infra": frozenset({"create", "destroy", "list", "show"}),
    "sandbox": frozenset({"build", "connect", "disconnect", "doctor", "status"}),
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
SandboxContextLoader = Callable[[], SandboxConnection | None]


class InteractiveShell:
    """Run known Verisentinel commands inside one persistent terminal session."""

    def __init__(
        self,
        *,
        repository: ShellSessionRepository,
        executor: CommandExecutor,
        terminal: TerminalUI | None = None,
        reader: ShellReader | None = None,
        dev_mode: bool = False,
        sandbox_context_loader: SandboxContextLoader | None = None,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.terminal = terminal or TerminalUI()
        self.reader = reader or self.terminal.read_shell_command
        self.dev_mode = dev_mode
        self.sandbox_context_loader = sandbox_context_loader

    def run(self, resume_session_id: str | None = None) -> int:
        """Start or resume a session and process input until explicit exit or EOF."""
        session = (
            self.repository.resume(resume_session_id)
            if resume_session_id
            else self.repository.create()
        )
        self.terminal.shell_started(
            session,
            resumed=resume_session_id is not None,
            dev_mode=self.dev_mode,
        )
        sandbox = self._load_sandbox_context()
        if sandbox is not None:
            self.terminal.sandbox_shell_entered(sandbox)
        while True:
            try:
                raw = self.reader(self.terminal.shell_prompt(session, sandbox))
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
                "back",
                "help",
                "quit",
            }:
                session, should_exit, sandbox = self._slash_command(
                    stripped,
                    session,
                    sandbox,
                )
                if should_exit:
                    return 0
                continue

            try:
                arguments = normalize_shell_command(stripped)
            except ValueError as error:
                self.terminal.shell_error(str(error))
                continue
            allowed_commands = _TOP_LEVEL_COMMANDS | ({"infra"} if self.dev_mode else set())
            if arguments[0] not in allowed_commands:
                self.terminal.shell_error(
                    "unknown command; this prompt accepts Verisentinel commands only"
                )
                continue
            if arguments[0] == "env" and sandbox is None:
                self.terminal.shell_error(
                    "env commands require a sandbox context; run sandbox connect first"
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
                continue
            if arguments[:2] == ["sandbox", "connect"]:
                sandbox = self._load_sandbox_context()
                if sandbox is not None:
                    self.terminal.sandbox_shell_entered(sandbox)
            elif arguments[:2] == ["sandbox", "disconnect"] or (
                arguments[:2] == ["infra", "destroy"]
            ):
                sandbox = self._load_sandbox_context()

    def _slash_command(
        self,
        raw: str,
        session: ShellSession,
        sandbox: SandboxConnection | None,
    ) -> tuple[ShellSession, bool, SandboxConnection | None]:
        command = raw.lower().removeprefix("/")
        if command == "back" or (
            sandbox is not None
            and not raw.startswith("/")
            and command in {"exit", "quit"}
        ):
            if sandbox is None:
                self.terminal.shell_error("no sandbox context is active")
            else:
                self.terminal.sandbox_shell_left(sandbox)
                sandbox = None
            return session, False, sandbox
        if command == "sandbox":
            sandbox = self._load_sandbox_context()
            if sandbox is None:
                self.terminal.shell_error("no sandbox connection is active")
            else:
                self.terminal.sandbox_shell_entered(sandbox)
            return session, False, sandbox
        if command in {"exit", "quit"}:
            closed = self.repository.close(session)
            self.terminal.shell_closed(closed)
            return closed, True, sandbox
        if command in {"help", "?", "--help"}:
            self.terminal.shell_help(
                dev_mode=self.dev_mode,
                sandbox_connected=sandbox is not None,
            )
            return session, False, sandbox
        if command in {"status", "history"}:
            self.terminal.session(session)
            return session, False, sandbox
        if command == "sessions":
            self.terminal.sessions(self.repository.list())
            return session, False, sandbox
        if command == "clear":
            self.terminal.clear()
            return session, False, sandbox
        if command == "new":
            self.repository.close(session)
            created = self.repository.create()
            self.terminal.shell_started(
                created,
                resumed=False,
                dev_mode=self.dev_mode,
            )
            return created, False, sandbox
        self.terminal.shell_error("unknown slash command; enter /help to see available actions")
        return session, False, sandbox

    def _load_sandbox_context(self) -> SandboxConnection | None:
        if self.sandbox_context_loader is None:
            return None
        return self.sandbox_context_loader()


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
