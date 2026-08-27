"""Terminal presentation and explicit operator input for Verisentinel."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from core.models import CandidateCard, DecisionKind
from execution.capsule.doctor import CapsuleDoctorReport
from execution.capsule.setup import CapsuleBuildReport
from runner.session import ShellSession

_THEME = Theme(
    {
        "accent": "bold cyan",
        "label": "dim",
        "success": "bold green",
        "warning": "bold yellow",
        "failure": "bold red",
        "muted": "grey62",
    }
)


@dataclass(frozen=True, slots=True)
class TerminalChoice:
    """One explicit operator decision captured by the terminal."""

    decision: DecisionKind
    candidate_id: str | None = None
    reason: str = ""


class TerminalUI:
    """Render the complete operator experience in the terminal."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(theme=_THEME, highlight=False)
        if console is not None:
            self.console.push_theme(_THEME)

    @classmethod
    def errors(cls) -> TerminalUI:
        """Create a renderer bound to standard error."""
        return cls(Console(stderr=True, theme=_THEME, highlight=False))

    def home(self, *, dev_mode: bool = False) -> None:
        """Show the terminal launchpad when no command is supplied."""
        commands = Table.grid(padding=(0, 3))
        commands.add_column(style="accent", no_wrap=True)
        commands.add_column()
        commands.add_row("shell", "Open the persistent interactive workspace")
        commands.add_row("scenario validate PATH", "Check a scenario before a run")
        commands.add_row("scenario run PATH", "Start an approval-gated scenario")
        commands.add_row("corpus build", "Build the authorization corpus")
        commands.add_row("corpus status", "Inspect the current corpus")
        commands.add_row("sandbox build", "Prepare the local execution capsule")
        commands.add_row("sandbox doctor", "Verify capsule isolation and readiness")
        commands.add_row("session list", "Inspect saved terminal sessions")
        if dev_mode:
            commands.add_row("infra create", "Provision disposable GCP infrastructure")
            commands.add_row("sandbox connect", "Connect the capsule to an infra ID")
        self.console.print(
            Panel(
                Group(
                    Text(
                        "VERISENTINEL DEV" if dev_mode else "VERISENTINEL",
                        style="accent",
                    ),
                    Text(
                        "Authorization analysis · coverage validation · "
                        "controlled simulation",
                        style="muted",
                    ),
                    Text(""),
                    commands,
                ),
                border_style="cyan",
                padding=(1, 2),
                subtitle="interactive terminals open the shell automatically",
                subtitle_align="left",
            )
        )

    def shell_started(
        self,
        session: ShellSession,
        *,
        resumed: bool,
        dev_mode: bool = False,
    ) -> None:
        """Render the compact header for an interactive terminal workspace."""
        state = "RESUMED" if resumed else "NEW SESSION"
        if dev_mode:
            state = f"DEV · {state}"
        content = Group(
            self._heading("VERISENTINEL", state, "success"),
            Text(session.session_id, style="muted"),
            Text(""),
            Text("Describe an operation or enter /help for commands."),
        )
        self.console.print(
            Panel(
                content,
                border_style="cyan",
                padding=(1, 2),
                subtitle="approval-gated · no system shell",
                subtitle_align="left",
            )
        )

    def shell_prompt(self, session: ShellSession) -> str:
        """Return the styled prompt for a terminal session."""
        short_id = session.session_id.removeprefix("session_")[:8]
        return f"[muted]{short_id}[/muted] [accent]›[/accent] "

    def read_shell_command(self, prompt: str) -> str:
        """Read one interactive command from the terminal."""
        return self.console.input(prompt)

    def shell_help(self, *, dev_mode: bool = False) -> None:
        """Render command and slash-command guidance inside the shell."""
        commands = Table.grid(padding=(0, 3))
        commands.add_column(style="accent", no_wrap=True)
        commands.add_column()
        commands.add_row("build corpus", "Build the authorization corpus")
        commands.add_row("build sandbox", "Prepare the execution capsule")
        commands.add_row("check sandbox", "Run capsule readiness checks")
        commands.add_row("validate scenario PATH", "Validate a scenario file")
        commands.add_row("run scenario PATH …", "Start the approval-gated workflow")
        commands.add_row("session list", "List terminal sessions")
        if dev_mode:
            commands.add_row("infra create …", "Provision a disposable GCP target")
            commands.add_row("infra list", "List development infrastructure")
            commands.add_row("sandbox connect ID", "Select a target for approved actions")

        slash = Table.grid(padding=(0, 3))
        slash.add_column(style="accent", no_wrap=True)
        slash.add_column()
        slash.add_row("/status", "Show this session")
        slash.add_row("/history", "Show sanitized command outcomes")
        slash.add_row("/sessions", "List all sessions")
        slash.add_row("/new", "Close this session and start another")
        slash.add_row("/clear", "Clear the terminal")
        slash.add_row("/exit", "Save and leave")
        self.console.print(
            Panel(
                Group(
                    Text("COMMANDS", style="label"),
                    commands,
                    Text(""),
                    Text("SESSION", style="label"),
                    slash,
                ),
                title="[accent]HELP[/accent]",
                title_align="left",
                border_style="cyan",
                padding=(1, 1),
            )
        )

    def shell_error(self, message: str) -> None:
        """Render a recoverable shell input error."""
        text = Text("INPUT  ", style="failure")
        text.append(message)
        self.console.print(text)

    def shell_command_failed(self, exit_code: int) -> None:
        """Keep the session active after a command-level failure."""
        self.console.print(
            f"[warning]Command exited with {exit_code}.[/warning] Session remains active."
        )

    def shell_interrupted(self) -> None:
        """Explain prompt interruption without closing the session."""
        self.console.print(
            "[warning]Input cancelled.[/warning] Enter /exit or press Ctrl+D to leave."
        )

    def shell_closed(self, session: ShellSession) -> None:
        """Render a compact saved-session confirmation."""
        self.console.print(
            f"[muted]Session {session.session_id} saved · "
            f"{session.command_count} commands[/muted]"
        )

    def clear(self) -> None:
        """Clear the active terminal display."""
        self.console.clear()

    def success(self, title: str, detail: str | None = None) -> None:
        """Render a compact successful outcome."""
        content = Text(title, style="success")
        if detail:
            content.append(f"\n{detail}")
        self.console.print(Panel(content, border_style="green", padding=(0, 1)))

    def error(self, category: str, message: str) -> None:
        """Render a bounded failure without a traceback."""
        content = Text(category, style="failure")
        content.append(f"\n{message}")
        self.console.print(Panel(content, border_style="red", padding=(0, 1)))

    def authentication(
        self,
        *,
        source_kind: str,
        principal: str,
        expires_at: str,
    ) -> None:
        """Render verified, public credential metadata."""
        self._key_values(
            "AUTHENTICATION",
            (
                ("Source", source_kind),
                ("Principal", principal),
                ("Expires", expires_at),
            ),
            state="VERIFIED",
            state_style="success",
        )

    def corpus(
        self,
        *,
        available: bool,
        matrix_version: str | None = None,
        permission_count: int = 0,
        detection_count: int = 0,
        technique_count: int = 0,
    ) -> None:
        """Render the current corpus summary."""
        if not available:
            self._key_values(
                "CORPUS",
                (("Next", "Run verisentinel corpus build"),),
                state="NOT BUILT",
                state_style="warning",
            )
            return
        self._key_values(
            "CORPUS",
            (
                ("Matrix", matrix_version or "unknown"),
                ("Permissions", str(permission_count)),
                ("Detections", str(detection_count)),
                ("Techniques", str(technique_count)),
            ),
            state="READY",
            state_style="success",
        )

    def sandbox_build(self, report: CapsuleBuildReport) -> None:
        """Render resources prepared for the local capsule."""
        network_status = "created" if report.network_created else "available"
        self._key_values(
            "SANDBOX",
            (
                ("Image", report.image),
                ("Network", report.network),
                ("Network state", network_status),
                ("Next", "Run verisentinel sandbox doctor"),
            ),
            state="BUILT",
            state_style="success",
        )

    def sandbox_doctor(self, report: CapsuleDoctorReport) -> None:
        """Render every capsule readiness check in one scan-friendly table."""
        table = Table(box=None, expand=True, padding=(0, 1))
        table.add_column("", width=4, no_wrap=True)
        table.add_column("CHECK", style="label", no_wrap=True)
        table.add_column("DETAIL")
        for check in report.checks:
            marker = Text("PASS" if check.passed else "FAIL")
            marker.stylize("success" if check.passed else "failure")
            table.add_row(marker, check.name, check.detail)
        state = "READY" if report.available else "ACTION REQUIRED"
        style = "success" if report.available else "warning"
        self.console.print(
            Panel(
                Group(
                    self._heading("SANDBOX", state, style),
                    Text(f"Provider  {report.provider}", style="muted"),
                    Text(report.detail, style="muted"),
                    Text(""),
                    table,
                ),
                border_style="green" if report.available else "yellow",
                padding=(1, 1),
            )
        )

    def sessions(self, sessions: tuple[ShellSession, ...]) -> None:
        """Render persisted terminal sessions in recency order."""
        if not sessions:
            self._key_values(
                "SESSIONS",
                (("Next", "Run verisentinel shell"),),
                state="EMPTY",
                state_style="muted",
            )
            return
        table = Table.grid(expand=True, padding=(0, 0, 1, 0))
        table.add_column()
        for session in sessions:
            identifier = Text(session.session_id, style="accent")
            metadata = Text(
                f"{session.status.value} · {session.command_count} commands · "
                f"{session.updated_at.isoformat(timespec='seconds')}",
                style="muted",
            )
            table.add_row(Group(identifier, metadata))
        self.console.print(
            Panel(
                table,
                title="[accent]SESSIONS[/accent]",
                title_align="left",
                border_style="cyan",
                padding=(1, 1),
            )
        )

    def session(self, session: ShellSession) -> None:
        """Render one terminal session and its sanitized command outcomes."""
        details = Table.grid(padding=(0, 2))
        details.add_column(style="label", no_wrap=True)
        details.add_column()
        details.add_row("Session", session.session_id)
        details.add_row("Status", session.status.value)
        details.add_row("Created", session.created_at.isoformat(timespec="seconds"))
        details.add_row("Updated", session.updated_at.isoformat(timespec="seconds"))
        details.add_row("Commands", str(session.command_count))

        history = Table(box=None, expand=True, padding=(0, 1))
        history.add_column("#", style="muted", justify="right", width=4)
        history.add_column("OPERATION")
        history.add_column("EXIT", justify="right", width=4)
        for event in session.events:
            history.add_row(str(event.sequence), event.operation, str(event.exit_code))
        body = Group(details, Text(""), history) if session.events else details
        self.console.print(
            Panel(
                body,
                title="[accent]SESSION[/accent]",
                title_align="left",
                border_style="cyan",
                padding=(1, 1),
            )
        )

    def run_started(
        self,
        *,
        engagement_id: str,
        provider: str,
        trace_path: str,
        conversation_path: str,
    ) -> None:
        """Render identifiers for an initialized planner run."""
        self._key_values(
            "SCENARIO RUN",
            (
                ("Engagement", engagement_id),
                ("Provider", provider),
                ("Trace", trace_path),
                ("Model log", conversation_path),
            ),
            state="ACTIVE",
            state_style="accent",
        )

    def candidates(self, candidates: tuple[CandidateCard, ...]) -> None:
        """Render admissible candidates and their validation evidence."""
        if not candidates:
            self.console.print(
                Panel(
                    "No admissible candidates are available in this cycle.",
                    title="[accent]REVIEW[/accent]",
                    border_style="yellow",
                )
            )
            return
        heading = Text("REVIEW QUEUE", style="accent")
        heading.append("  VALIDATED · APPROVAL REQUIRED", style="warning")
        self.console.print(Padding(heading, (1, 0, 0, 0)))
        for index, card in enumerate(candidates, start=1):
            proposal = card.proposal
            validation = card.validation
            details = Table.grid(padding=(0, 2))
            details.add_column(style="label", no_wrap=True)
            details.add_column()
            details.add_row("Technique", proposal.technique_id)
            details.add_row("Action", proposal.action_id)
            details.add_row("Identity", proposal.identity)
            details.add_row("Target", proposal.target)
            details.add_row("Required", _joined(card.required_permissions))
            details.add_row("Covered", _joined(validation.covered_permissions))
            details.add_row("Uncovered", _joined(validation.uncovered_permissions))
            details.add_row("Detections", _joined(validation.matching_detection_ids))
            details.add_row("Capabilities", _joined(card.expected_capabilities))
            details.add_row("Rationale", proposal.rationale)
            details.add_row("Validation", validation.explanation)
            title = card.technique_title or proposal.technique_id
            panel_title = Text()
            panel_title.append(f"{index:02d}", style="accent")
            panel_title.append(f"  {title}")
            self.console.print(
                Panel(
                    details,
                    title=panel_title,
                    title_align="left",
                    border_style="cyan",
                    padding=(1, 1),
                )
            )

    def choice_prompt(self) -> str:
        """Prompt for one decision without introducing an approval default."""
        self.console.print(
            "[accent]1–3[/accent] approve   [accent]r1–r3[/accent] reject   "
            "[accent]a[/accent] alternatives   [accent]x[/accent] reject all   "
            "[accent]q[/accent] terminate"
        )
        return self.console.input("[accent]Decision › [/accent]")

    def invalid_choice(self, message: str) -> None:
        """Render a recoverable operator input error."""
        self.console.print(f"[failure]Invalid choice[/failure]  {message}")

    def cycle_message(self, message: str) -> None:
        """Render the outcome of one operator decision."""
        self.console.print(Panel(Text(message), border_style="cyan", padding=(0, 1)))

    def finished(self, *, failed: bool) -> None:
        """Render the terminal state of a scenario run."""
        if failed:
            self.error("RUN FAILED", "The engagement ended in a failed state.")
        else:
            self.success("RUN COMPLETE", "The engagement is no longer awaiting approval.")

    def _key_values(
        self,
        title: str,
        rows: tuple[tuple[str, str], ...],
        *,
        state: str,
        state_style: str,
    ) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="label", no_wrap=True)
        table.add_column(overflow="fold")
        for label, value in rows:
            table.add_row(label, value)
        self.console.print(
            Panel(
                Group(self._heading(title, state, state_style), Text(""), table),
                border_style="cyan",
                padding=(1, 1),
            )
        )

    @staticmethod
    def _heading(title: str, state: str, state_style: str) -> Text:
        heading = Text(title, style="accent")
        heading.append("  ")
        heading.append(state, style=state_style)
        return heading


def parse_choice(raw: str, candidates: tuple[CandidateCard, ...]) -> TerminalChoice:
    """Parse a terminal choice without ever defaulting to approval."""
    value = raw.strip().lower()
    if value in {"q", "quit", "terminate"}:
        return TerminalChoice(DecisionKind.TERMINATE)
    if value in {"a", "alternatives"}:
        return TerminalChoice(DecisionKind.REQUEST_ALTERNATIVES)
    if value in {"x", "reject-all"}:
        return TerminalChoice(DecisionKind.REJECT_ALL)

    decision = DecisionKind.APPROVE
    number = value
    if value.startswith("r"):
        decision = DecisionKind.REJECT
        number = value[1:].strip()
    if not number.isdigit():
        raise ValueError("enter 1-3, r1-r3, a, x, or q")
    index = int(number) - 1
    if index < 0 or index >= len(candidates):
        raise ValueError("the selected candidate number is not displayed")
    return TerminalChoice(decision, candidates[index].proposal.candidate_id)


def _joined(values: tuple[str, ...]) -> str:
    return ", ".join(values) or "none"
