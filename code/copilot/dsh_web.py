"""Full DeepSeek Harness Web integration for interactive coding sessions."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from websockets.sync.client import connect as websocket_connect

from copilot.protocol import CopilotSession, CopilotTurn
from core.tracing import DebugTrace

_READY_PATTERN = re.compile(r"dsh web: (http://[^\s]+)")
_MAX_ERROR_CHARACTERS = 1_024
_MAX_OUTPUT_LINES = 20


class DshWebUnavailable(RuntimeError):
    """The configured full DSH Web application cannot be used."""


class DshWebRpcError(RuntimeError):
    """A bounded failure returned by the authenticated DSH Host API."""


class DshWebLauncher:
    """Start one owned full DSH Web process from a pinned source checkout."""

    def __init__(
        self,
        checkout: Path,
        home: Path,
        *,
        startup_timeout_seconds: float = 90.0,
        pnpm: str | None = None,
    ) -> None:
        self.checkout = checkout.resolve()
        self.home = home.resolve()
        self.startup_timeout_seconds = startup_timeout_seconds
        self.pnpm = pnpm
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._recent_output: deque[str] = deque(maxlen=_MAX_OUTPUT_LINES)

    def start(self) -> str:
        """Start DSH and return its process-authenticated loopback URL."""
        if self._process is not None:
            raise DshWebUnavailable("the DSH Web process is already running")
        if not (self.checkout / "package.json").is_file():
            raise DshWebUnavailable(
                f"the DSH source checkout is invalid: {self.checkout}"
            )
        if not (self.checkout / "node_modules").is_dir():
            raise DshWebUnavailable(
                "the DSH source checkout is not installed; run pnpm install and pnpm run build"
            )
        if not self.home.is_dir():
            raise DshWebUnavailable(
                f"DSH_HOME does not exist or is not a directory: {self.home}"
            )
        pnpm = self.pnpm or shutil.which("pnpm")
        if pnpm is None:
            raise DshWebUnavailable("pnpm is required to launch the full DSH Web application")

        environment = os.environ.copy()
        environment["DSH_HOME"] = str(self.home)
        try:
            self._process = subprocess.Popen(
                (pnpm, "dsh", "web", "--no-open", "--port", "0"),
                cwd=self.checkout,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            raise DshWebUnavailable("the full DSH Web process could not start") from error
        threading.Thread(target=self._drain_output, daemon=True).start()

        deadline = time.monotonic() + self.startup_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise DshWebUnavailable(
                    "the full DSH Web process did not publish a readiness URL"
                )
            try:
                line = self._lines.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if self._process.poll() is None:
                    continue
                detail = self._bounded_output()
                self.close()
                raise DshWebUnavailable(
                    "the full DSH Web process exited before readiness"
                    + (f": {detail}" if detail else "")
                ) from None
            match = _READY_PATTERN.search(line)
            if match is not None:
                return match.group(1)

    def close(self) -> None:
        """Stop only the DSH process group created by this launcher."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def _drain_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            normalized = line.rstrip()
            self._recent_output.append(normalized)
            self._lines.put(normalized)

    def _bounded_output(self) -> str:
        output = " | ".join(self._recent_output)
        output = _READY_PATTERN.sub("dsh web: <redacted>", output)
        return output[-_MAX_ERROR_CHARACTERS:]


class DshWebClient:
    """Authenticated client for the supported full DSH Web Host API."""

    def __init__(
        self,
        launch_url: str,
        *,
        timeout_seconds: float,
        http_client: httpx.Client | None = None,
        socket_factory: Callable[..., Any] = websocket_connect,
    ) -> None:
        parsed = urlparse(launch_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parsed.query
        ):
            raise DshWebUnavailable(
                "DSH Web must publish an authenticated loopback HTTP URL"
            )
        self.launch_url = launch_url
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.timeout_seconds = timeout_seconds
        self._http = http_client or httpx.Client(
            timeout=min(timeout_seconds, 30.0),
            follow_redirects=False,
        )
        self._owns_http = http_client is None
        self._socket_factory = socket_factory
        self._cookie = self._authenticate()

    def close(self) -> None:
        """Close the owned HTTP connection pool."""
        if self._owns_http:
            self._http.close()

    def create_workspace(self, path: Path) -> str:
        """Create or resolve one DSH workspace and return its opaque id."""
        value = self._rpc("workspace/create", {"request": {"path": str(path)}})
        workspace = value.get("workspace") if isinstance(value, dict) else None
        workspace_id = workspace.get("workspaceId") if isinstance(workspace, dict) else None
        if not isinstance(workspace_id, str) or not workspace_id:
            raise DshWebRpcError("workspace/create returned no workspace id")
        return workspace_id

    def create_session(self, workspace_id: str, session_id: str) -> None:
        """Create or idempotently resume one named DSH session."""
        self._rpc(
            "session/create",
            {
                "request": {
                    "workspaceId": workspace_id,
                    "sessionId": session_id,
                }
            },
        )

    def select_model(self, session_id: str, provider: str, model: str) -> None:
        """Select the configured provider route for one DSH session."""
        self._rpc(
            "session/selectModel",
            {
                "request": {
                    "sessionId": session_id,
                    "provider": provider,
                    "model": model,
                }
            },
        )

    def run_turn(self, session_id: str, prompt: str) -> CopilotTurn:
        """Submit one prompt and follow its exact durable turn to completion."""
        request_id = str(uuid4())
        stream_id = f"verisentinel-{uuid4()}"
        websocket_url = self.origin.replace("http://", "ws://", 1) + "/api/remote.mux"
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with self._socket_factory(
                websocket_url,
                additional_headers={"Cookie": self._cookie},
                open_timeout=min(self.timeout_seconds, 30.0),
                close_timeout=2.0,
            ) as socket:
                socket.send(
                    json.dumps(
                        {
                            "type": "open",
                            "streamId": stream_id,
                            "endpoint": "session/follow",
                            "payload": {
                                "args": {
                                    "request": {
                                        "address": {
                                            "kind": "session",
                                            "sessionId": session_id,
                                        }
                                    }
                                }
                            },
                        }
                    )
                )
                self._await_snapshot(socket, stream_id, deadline)
                self._rpc(
                    "session/prompt",
                    {
                        "request": {
                            "requestId": request_id,
                            "sessionId": session_id,
                            "mode": "queue",
                            "content": [{"type": "text", "text": prompt}],
                        }
                    },
                )
                return self._await_turn(socket, stream_id, request_id, deadline)
        except DshWebRpcError:
            raise
        except TimeoutError as error:
            raise DshWebRpcError("the DSH session turn exceeded its timeout") from error
        except (OSError, ValueError) as error:
            raise DshWebRpcError("the DSH session event stream failed") from error

    def _authenticate(self) -> str:
        try:
            response = self._http.get(self.launch_url)
        except httpx.HTTPError as error:
            raise DshWebUnavailable("DSH Web authentication failed") from error
        if response.status_code != 303:
            raise DshWebUnavailable(
                f"DSH Web authentication returned HTTP {response.status_code}"
            )
        cookies = [f"{cookie.name}={cookie.value}" for cookie in self._http.cookies.jar]
        if not cookies:
            raise DshWebUnavailable("DSH Web authentication returned no session cookie")
        return "; ".join(cookies)

    def _rpc(self, endpoint: str, args: dict[str, object]) -> dict[str, Any]:
        try:
            response = self._http.post(
                f"{self.origin}/api/{endpoint}",
                headers={"content-type": "application/json", "cookie": self._cookie},
                json={
                    "type": "client-request",
                    "rpcId": f"verisentinel-{uuid4()}",
                    "method": endpoint,
                    "payload": {"args": args},
                },
            )
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise DshWebRpcError(f"{endpoint} failed over the DSH Host API") from error
        result = document.get("result") if isinstance(document, dict) else None
        if not isinstance(result, dict):
            raise DshWebRpcError(f"{endpoint} returned an invalid DSH response")
        if result.get("ok") is not True:
            error = result.get("error")
            code = error.get("code") if isinstance(error, dict) else "unknown"
            message = error.get("message") if isinstance(error, dict) else "request rejected"
            detail = f"{code}: {message}"[:_MAX_ERROR_CHARACTERS]
            raise DshWebRpcError(f"{endpoint} failed: {detail}")
        value = result.get("value")
        if not isinstance(value, dict):
            raise DshWebRpcError(f"{endpoint} returned no DSH value")
        return value

    @staticmethod
    def _await_snapshot(socket: Any, stream_id: str, deadline: float) -> None:
        while True:
            frame = _receive_frame(socket, deadline)
            if frame.get("streamId") != stream_id:
                continue
            if frame.get("type") == "error":
                raise DshWebRpcError("session/follow rejected the DSH event stream")
            value = frame.get("value")
            if (
                frame.get("type") == "item"
                and isinstance(value, dict)
                and value.get("type") == "snapshot"
            ):
                return

    @staticmethod
    def _await_turn(
        socket: Any,
        stream_id: str,
        request_id: str,
        deadline: float,
    ) -> CopilotTurn:
        accepted = False
        response = ""
        while True:
            frame = _receive_frame(socket, deadline)
            if frame.get("streamId") != stream_id:
                continue
            if frame.get("type") == "error":
                raise DshWebRpcError("session/follow failed during the DSH turn")
            if frame.get("type") == "end":
                raise DshWebRpcError("session/follow ended before the DSH turn completed")
            value = frame.get("value")
            event = value.get("event") if isinstance(value, dict) else None
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            data = event.get("data")
            if event_type == "user/message" and _request_id(data) == request_id:
                accepted = True
                response = ""
                continue
            if not accepted:
                continue
            if event_type == "assistant/message":
                response = _assistant_text(data) or response
                continue
            if event_type != "turn/end":
                continue
            reason = data.get("reason") if isinstance(data, dict) else None
            kind = reason.get("kind") if isinstance(reason, dict) else None
            if kind == "error":
                error = reason.get("error")
                code = error.get("code") if isinstance(error, dict) else "MODEL_ERROR"
                message = error.get("message") if isinstance(error, dict) else "model turn failed"
                return CopilotTurn(
                    response=f"{code}: {message}"[:_MAX_ERROR_CHARACTERS],
                    finish_reason="error",
                )
            return CopilotTurn(response=response, finish_reason=str(kind or "completed"))


class _DshWebSession:
    """One persistent session presented through the full DSH Web application."""

    def __init__(self, client: DshWebClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def run(self, prompt: str) -> CopilotTurn:
        return self._client.run_turn(self.session_id, prompt)


class DshWebCopilotHarness:
    """Use the complete DSH Web product as the persistent coding harness."""

    def __init__(
        self,
        *,
        checkout: Path,
        home: Path,
        provider: str,
        model: str,
        workspace_root: Path,
        timeout_seconds: float,
        handoff: Callable[[str, str, Path], None],
        trace: DebugTrace | None = None,
        launcher: DshWebLauncher | None = None,
        client: DshWebClient | None = None,
    ) -> None:
        self.checkout = checkout.resolve()
        self.home = home.resolve()
        self.provider = provider
        self._model = model
        self.workspace_root = workspace_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.handoff = handoff
        self.trace = trace
        self._launcher = launcher or DshWebLauncher(self.checkout, self.home)
        self._client = client
        self._sessions: dict[str, _DshWebSession] = {}
        self._launch_url: str | None = client.launch_url if client is not None else None

    @property
    def model(self) -> str:
        return self._model

    def require_ready(self) -> None:
        """Start and authenticate the full DSH Web application once."""
        if self._client is not None:
            return
        self._launch_url = self._launcher.start()
        try:
            self._client = DshWebClient(
                self._launch_url,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception:
            self._launcher.close()
            self._launch_url = None
            raise
        if self.trace is not None:
            self.trace.emit(
                "copilot",
                "ready",
                adapter="dsh-web",
                provider=self.provider,
                model=self.model,
            )

    def session(self, session_id: str) -> CopilotSession:
        """Create or resume a DSH workspace session and hand it to the operator."""
        self.require_ready()
        if session_id in self._sessions:
            return self._sessions[session_id]
        if self._client is None:
            raise DshWebUnavailable("the DSH Web client is unavailable")
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace_id = self._client.create_workspace(self.workspace_root)
        self._client.create_session(workspace_id, session_id)
        self._client.select_model(session_id, self.provider, self.model)
        if self._launch_url is None:
            raise DshWebUnavailable("the DSH Web launch URL is unavailable")
        self.handoff(self._launch_url, session_id, self.workspace_root)
        session = _DshWebSession(self._client, session_id)
        self._sessions[session_id] = session
        if self.trace is not None:
            self.trace.emit(
                "copilot",
                "session_handed_off",
                session_id=session_id,
                workspace=str(self.workspace_root),
            )
        return session

    def close(self) -> None:
        """Close the authenticated client and the owned DSH process."""
        if self._client is not None:
            self._client.close()
        self._client = None
        self._sessions = {}
        self._launch_url = None
        self._launcher.close()


def _receive_frame(socket: Any, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    raw = socket.recv(timeout=remaining)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("DSH event frame is not an object")
    return document


def _request_id(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    source = data.get("source")
    if not isinstance(source, dict):
        message = data.get("message")
        source = message.get("source") if isinstance(message, dict) else None
    request_id = source.get("rpcId") if isinstance(source, dict) else None
    return request_id if isinstance(request_id, str) else None


def _assistant_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else data.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(part for part in parts if part).strip()
