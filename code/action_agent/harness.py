"""Persistent coding-harness authoring with deterministic repair preflight."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from action_agent.preflight import ActionPreflight, ActionPreflightError
from action_agent.service import build_action_command
from copilot.protocol import CopilotHarness, CopilotSession
from core.models import ActionArtifact, ActionAuthorRequest, ActionCommand, CandidateCard
from core.tracing import DebugTrace

_MAX_ARTIFACT_BYTES = 65_536

_WORKSPACE_INSTRUCTIONS = """# Action workspace

You are implementing one already-selected action inside an isolated coding harness.

- Read `action-request.json` before changing anything.
- Create exactly one complete file named `action.py` in this workspace.
- The file must read `/run/verisentinel/spec.json` and
  `/run/verisentinel/credential` at execution time.
- Build the nine-field typed action envelope from the execution spec; never post
  the raw execution spec.
- POST only to `http://verisentinel-mock:8080/execute` with a positive timeout.
- Write only the gateway JSON observation to stdout.
- Use only `json`, `pathlib`, `sys`, `urllib.error`, and `urllib.request`.
- Do not access a cloud API, credential, repository path, or external endpoint.
- Inspect and test your work locally when a relevant tool is available. The
  controlling harness performs an additional isolated contract test.

You may create temporary test files inside this workspace, but only `action.py`
is eligible for operator review and controlled execution.
"""


class HarnessActionAgent:
    """Use one resumable harness session to author, test, and repair an action."""

    def __init__(
        self,
        harness: CopilotHarness,
        workspace: Path,
        *,
        preflight: ActionPreflight | None = None,
        maximum_repairs: int = 3,
        trace: DebugTrace | None = None,
    ) -> None:
        if maximum_repairs < 1:
            raise ValueError("maximum_repairs must be positive")
        self.harness = harness
        self.workspace = workspace.resolve()
        self.preflight = preflight or ActionPreflight()
        self.maximum_repairs = maximum_repairs
        self.trace = trace
        self._sessions: dict[str, CopilotSession] = {}
        self._approval_sessions: dict[str, CopilotSession] = {}

    def author(self, request: ActionAuthorRequest) -> ActionCommand:
        """Return only a file that passed the isolated action contract test."""
        self._prepare_workspace(request)
        session = self._session(request.engagement_id)
        prompt = self._author_prompt(request)
        last_error = "action.py was not created"
        for attempt in range(1, self.maximum_repairs + 1):
            self._emit(
                "action_harness_turn_started",
                engagement_id=request.engagement_id,
                session_id=session.session_id,
                attempt=attempt,
            )
            turn = session.run(prompt)
            if turn.finish_reason == "error":
                raise RuntimeError(
                    "the coding harness model turn failed before producing a reviewable file"
                )
            try:
                content = self._read_artifact()
                self.preflight.validate(request, content)
            except (ActionPreflightError, OSError, ValueError) as error:
                last_error = str(error)[:2048]
                self._emit(
                    "action_preflight_failed",
                    level="warning",
                    engagement_id=request.engagement_id,
                    session_id=session.session_id,
                    attempt=attempt,
                    error=last_error,
                )
                prompt = (
                    "The controlling harness rejected action.py. Fix the file, "
                    "recheck it, and finish only after the issue is resolved.\n\n"
                    f"Deterministic failure:\n{last_error}"
                )
                continue
            artifact = self._artifact(content)
            command = build_action_command(request, artifact)
            self._approval_sessions[request.approval_id] = session
            self._emit(
                "action_preflight_passed",
                engagement_id=request.engagement_id,
                session_id=session.session_id,
                attempt=attempt,
                artifact_digest=artifact.digest,
            )
            return command.model_copy(
                update={
                    "tool_installation": (
                        f"Authored by {artifact.source_model} in isolated session "
                        f"{session.session_id}; contract preflight passed; not "
                        "promoted to the execution workspace until approval."
                    )
                }
            )
        raise RuntimeError(
            "the coding harness could not produce a preflight-valid action.py "
            f"after {self.maximum_repairs} turn(s): {last_error}"
        )

    def answer(self, card: CandidateCard, question: str) -> str:
        """Continue the same session without executing the pending action."""
        command = card.action_command
        if command is None or command.approval_id is None:
            raise ValueError("action question requires a pending command")
        session = self._approval_sessions.get(command.approval_id)
        if session is None:
            raise ValueError("the pending action has no resumable harness session")
        turn = session.run(
            "Answer the operator's message directly using the current workspace "
            "and action-request.json. Do not execute the action or access a "
            f"credential. Do not change action.py during this explanatory turn.\n\n"
            f"Operator message:\n{question}"
        )
        if turn.finish_reason == "error" or not turn.response.strip():
            raise RuntimeError("the coding harness did not return an explanation")
        return turn.response.strip()

    def _prepare_workspace(self, request: ActionAuthorRequest) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.workspace / "AGENTS.md").write_text(
            _WORKSPACE_INSTRUCTIONS,
            encoding="utf-8",
        )
        (self.workspace / "action-request.json").write_text(
            json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _read_artifact(self) -> str:
        path = self.workspace / "action.py"
        encoded = path.read_bytes()
        if not encoded or len(encoded) > _MAX_ARTIFACT_BYTES:
            raise ValueError("action.py is empty or exceeds its size limit")
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("action.py is not UTF-8 text") from None

    def _artifact(self, content: str) -> ActionArtifact:
        return ActionArtifact(
            content=content,
            digest="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_model=f"DeepSeek Harness · {self.harness.model}",
            rationale=(
                "Persistent coding session authored the file and passed the "
                "isolated typed-envelope contract preflight."
            ),
        )

    def _session(self, engagement_id: str) -> CopilotSession:
        session_id = f"action-{engagement_id}"
        if session_id not in self._sessions:
            self._sessions[session_id] = self.harness.session(session_id)
        return self._sessions[session_id]

    @staticmethod
    def _author_prompt(request: ActionAuthorRequest) -> str:
        feedback = "\n".join(request.repair_feedback[-10:]) or "none"
        return (
            "Read AGENTS.md and action-request.json. Implement action.py for the "
            "selected action. Inspect the exact execution-spec shape described in "
            "the request, test what you safely can in this workspace, and repair "
            "all failures before answering. No action is approved or executing.\n\n"
            f"Prior bounded repair feedback:\n{feedback}"
        )

    def _emit(self, event: str, *, level: str = "info", **details: object) -> None:
        if self.trace is not None:
            self.trace.emit("action_agent", event, level=level, **details)
