"""Tests for the full DSH Web copilot integration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import copilot.dsh_web as dsh_web_module
from copilot.dsh_web import (
    DshWebClient,
    DshWebCopilotHarness,
    DshWebRpcError,
    DshWebUnavailable,
)
from copilot.protocol import CopilotTurn


class FakeSocket:
    def __init__(self, frames: list[dict[str, object]]) -> None:
        self.frames = frames
        self.sent: list[dict[str, object]] = []

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    def recv(self, *, timeout: float) -> str:
        assert timeout > 0
        if not self.frames:
            raise TimeoutError
        return json.dumps(self.frames.pop(0))


def _frame(value: dict[str, object]) -> dict[str, object]:
    return {
        "type": "item",
        "streamId": "verisentinel-fixed-id",
        "value": value,
    }


def _event(event_type: str, data: dict[str, object]) -> dict[str, object]:
    return {
        "type": "event",
        "event": {"type": event_type, "seq": 1, "time": 1, "data": data},
    }


def test_dsh_web_client_runs_one_durable_session_turn(monkeypatch) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                303,
                headers={"set-cookie": "dsh-auth=fixed; Path=/; HttpOnly"},
            )
        endpoint = request.url.path.removeprefix("/api/")
        requests.append(endpoint)
        return httpx.Response(200, json={"result": {"ok": True, "value": {"accepted": True}}})

    socket = FakeSocket(
        [
            _frame({"type": "snapshot", "cursor": 0, "records": []}),
            _frame(
                _event(
                    "user/message",
                    {"source": {"kind": "user", "rpcId": "fixed-id"}},
                )
            ),
            _frame(
                _event(
                    "assistant/message",
                    {
                        "message": {
                            "content": [{"type": "text", "text": "action.py is ready"}]
                        }
                    },
                )
            ),
            _frame(_event("turn/end", {"reason": {"kind": "completed"}})),
        ]
    )
    monkeypatch.setattr(dsh_web_module, "uuid4", lambda: "fixed-id")
    client = DshWebClient(
        "http://127.0.0.1:3080/?token=process-token",
        timeout_seconds=30,
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
        socket_factory=lambda *args, **kwargs: socket,
    )

    turn = client.run_turn("session", "author the action")

    assert turn == CopilotTurn(response="action.py is ready", finish_reason="completed")
    assert requests == ["session/prompt"]
    assert socket.sent[0]["endpoint"] == "session/follow"


def test_dsh_web_client_preserves_bounded_model_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(303, headers={"set-cookie": "dsh-auth=fixed; Path=/"})
        return httpx.Response(200, json={"result": {"ok": True, "value": {"accepted": True}}})

    socket = FakeSocket(
        [
            _frame({"type": "snapshot", "cursor": 0, "records": []}),
            _frame(_event("user/message", {"source": {"rpcId": "fixed-id"}})),
            _frame(
                _event(
                    "turn/end",
                    {
                        "reason": {
                            "kind": "error",
                            "error": {"code": "QUOTA", "message": "Insufficient Balance"},
                        }
                    },
                )
            ),
        ]
    )
    monkeypatch.setattr(dsh_web_module, "uuid4", lambda: "fixed-id")
    client = DshWebClient(
        "http://localhost:3080/?token=process-token",
        timeout_seconds=30,
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
        socket_factory=lambda *args, **kwargs: socket,
    )

    turn = client.run_turn("session", "author the action")

    assert turn == CopilotTurn(
        response="QUOTA: Insufficient Balance",
        finish_reason="error",
    )


def test_dsh_web_client_rejects_non_loopback_launch_url() -> None:
    with pytest.raises(DshWebUnavailable, match="loopback"):
        DshWebClient(
            "https://example.test/?token=secret",
            timeout_seconds=30,
        )


class FakeClient:
    launch_url = "http://127.0.0.1:3080/?token=process-token"

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.closed = False

    def create_workspace(self, path: Path) -> str:
        self.calls.append(("workspace", path))
        return "workspace-id"

    def create_session(self, workspace_id: str, session_id: str) -> None:
        self.calls.append(("session", workspace_id, session_id))

    def select_model(self, session_id: str, provider: str, model: str) -> None:
        self.calls.append(("model", session_id, provider, model))

    def run_turn(self, session_id: str, prompt: str) -> CopilotTurn:
        self.calls.append(("turn", session_id, prompt))
        return CopilotTurn(response="done", finish_reason="completed")

    def close(self) -> None:
        self.closed = True


def test_dsh_web_harness_hands_off_one_persistent_session(tmp_path: Path) -> None:
    client = FakeClient()
    handoffs: list[tuple[str, str, Path]] = []
    harness = DshWebCopilotHarness(
        checkout=tmp_path / "checkout",
        home=tmp_path / "home",
        provider="google-vertex",
        model="gemini-3.7-flash",
        workspace_root=tmp_path / "workspace",
        timeout_seconds=30,
        handoff=lambda *values: handoffs.append(values),
        launcher=SimpleNamespace(start=lambda: client.launch_url, close=lambda: None),
        client=client,
    )

    first = harness.session("action-engagement")
    second = harness.session("action-engagement")
    turn = second.run("repair the file")

    assert first is second
    assert turn.response == "done"
    assert handoffs == [
        (client.launch_url, "action-engagement", (tmp_path / "workspace").resolve())
    ]
    assert client.calls == [
        ("workspace", (tmp_path / "workspace").resolve()),
        ("session", "workspace-id", "action-engagement"),
        ("model", "action-engagement", "google-vertex", "gemini-3.7-flash"),
        ("turn", "action-engagement", "repair the file"),
    ]


def test_dsh_web_rpc_reports_remote_code_without_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(303, headers={"set-cookie": "dsh-auth=fixed; Path=/"})
        return httpx.Response(
            200,
            json={
                "result": {
                    "ok": False,
                    "error": {"code": "UNKNOWN_MODEL", "message": "model unavailable"},
                }
            },
        )

    client = DshWebClient(
        "http://127.0.0.1:3080/?token=process-token",
        timeout_seconds=30,
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
    )

    with pytest.raises(DshWebRpcError, match="UNKNOWN_MODEL: model unavailable"):
        client.select_model("session", "google-vertex", "missing")
