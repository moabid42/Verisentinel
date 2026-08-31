"""Tests for persistent harness authoring and repair."""

from pathlib import Path

from action_agent.harness import HarnessActionAgent
from action_agent.preflight import ActionPreflightError
from copilot.protocol import CopilotTurn
from tests.unit.test_action_agent import VALID_ACTION_SOURCE, author_request


class ScriptedSession:
    def __init__(self, workspace: Path, sources: list[str]) -> None:
        self.workspace = workspace
        self.sources = sources
        self.prompts: list[str] = []
        self.session_id = "session"

    def run(self, prompt: str) -> CopilotTurn:
        self.prompts.append(prompt)
        if self.sources:
            (self.workspace / "action.py").write_text(
                self.sources.pop(0),
                encoding="utf-8",
            )
        return CopilotTurn(response="done", finish_reason="stop")


class FakeHarness:
    model = "fixture-model"

    def __init__(self, session: ScriptedSession) -> None:
        self.raw_session = session

    def require_ready(self) -> None:
        return None

    def session(self, session_id: str) -> ScriptedSession:
        del session_id
        return self.raw_session

    def close(self) -> None:
        return None


class FailOncePreflight:
    def __init__(self) -> None:
        self.calls = 0

    def validate(self, request, content: str) -> None:
        del request, content
        self.calls += 1
        if self.calls == 1:
            raise ActionPreflightError("request body is not the exact typed action envelope")


def test_harness_repairs_failed_preflight_in_the_same_session(tmp_path: Path) -> None:
    session = ScriptedSession(tmp_path, ["print('bad')\n", VALID_ACTION_SOURCE])
    preflight = FailOncePreflight()
    agent = HarnessActionAgent(
        FakeHarness(session),
        tmp_path,
        preflight=preflight,
    )

    command = agent.author(author_request())

    assert command.artifact is not None
    assert command.artifact.content == VALID_ACTION_SOURCE
    assert command.artifact.written is False
    assert preflight.calls == 2
    assert len(session.prompts) == 2
    assert "exact typed action envelope" in session.prompts[1]
    assert "contract preflight passed" in command.tool_installation


def test_harness_answers_in_the_authoring_session(tmp_path: Path) -> None:
    session = ScriptedSession(tmp_path, [VALID_ACTION_SOURCE])
    agent = HarnessActionAgent(
        FakeHarness(session),
        tmp_path,
        preflight=FailOncePreflight(),
    )
    command = agent.author(author_request())
    card = type("Card", (), {"action_command": command})()

    answer = agent.answer(card, "Why is this needed?")

    assert answer == "done"
    assert "Why is this needed?" in session.prompts[-1]
