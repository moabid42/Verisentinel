"""Tests for the pinned DeepSeek Harness adapter."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from copilot.deepseek import (
    DEEPSEEK_HARNESS_VERSION,
    DeepSeekCopilotHarness,
    DeepSeekHarnessUnavailable,
)


class FakeRawSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.prompts: list[str] = []

    def run(self, prompt: str, *, on_notification):
        self.prompts.append(prompt)
        on_notification(
            SimpleNamespace(
                method="session.status",
                payload={"status": "idle"},
            )
        )
        return SimpleNamespace(final_response="done", finish_reason="stop")


class FakeSdk:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.sessions: dict[str, FakeRawSession] = {}

    def start(self) -> None:
        self.started = True

    def start_session(self, session_id: str) -> FakeRawSession:
        return self.sessions.setdefault(session_id, FakeRawSession(session_id))

    def close(self) -> None:
        self.closed = True


def harness(tmp_path: Path, **overrides) -> DeepSeekCopilotHarness:
    runtime = tmp_path / "dsh"
    cordis = tmp_path / "cordis.yml"
    runtime.write_text("runtime", encoding="utf-8")
    cordis.write_text("config", encoding="utf-8")
    settings = {
        "api_key": "synthetic-model-key",
        "model": "model",
        "workspace_root": tmp_path / "workspace",
        "session_root": tmp_path / "sessions",
        "sdk_factory": FakeSdk,
        "installed_version": DEEPSEEK_HARNESS_VERSION,
        "runtime_bin": runtime,
        "cordis_path": cordis,
        "bubblewrap": "/usr/bin/bwrap",
    }
    settings.update(overrides)
    return DeepSeekCopilotHarness(**settings)


def test_deepseek_adapter_reuses_one_persistent_session(tmp_path: Path) -> None:
    adapter = harness(tmp_path)

    first = adapter.session("engagement")
    second = adapter.session("engagement")
    turn = second.run("repair the file")

    assert first is second
    assert turn.response == "done"
    assert turn.finish_reason == "stop"
    sdk = adapter._sdk
    assert isinstance(sdk, FakeSdk)
    assert sdk.started
    assert "synthetic-model-key" not in sdk.kwargs["launch_args_override"]


def test_deepseek_adapter_rejects_unpinned_sdk(tmp_path: Path) -> None:
    adapter = harness(tmp_path, installed_version="0.1.0")

    with pytest.raises(DeepSeekHarnessUnavailable, match="version mismatch"):
        adapter.require_ready()
