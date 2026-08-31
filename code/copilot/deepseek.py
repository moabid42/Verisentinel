"""Pinned DeepSeek Harness adapter with resumable isolated sessions."""

from __future__ import annotations

import importlib.metadata
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from copilot.protocol import CopilotSession, CopilotTurn
from core.tracing import DebugTrace

DEEPSEEK_HARNESS_VERSION = "0.1.1rc1"
DEEPSEEK_RUNTIME_IMAGE = (
    "ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517"
)
_MAX_RESPONSE_CHARACTERS = 16_384


class DeepSeekHarnessUnavailable(RuntimeError):
    """The optional pinned harness runtime cannot start safely."""


class _DeepSeekSession:
    """Translate the DeepSeek SDK session into the repository protocol."""

    def __init__(
        self,
        raw_session: Any,
        trace: DebugTrace | None,
    ) -> None:
        self._raw_session = raw_session
        self._trace = trace

    @property
    def session_id(self) -> str:
        return str(self._raw_session.id)

    def run(self, prompt: str) -> CopilotTurn:
        result = self._raw_session.run(
            prompt,
            on_notification=self._notification,
        )
        response = str(result.final_response or "")[:_MAX_RESPONSE_CHARACTERS]
        return CopilotTurn(
            response=response,
            finish_reason=(str(result.finish_reason) if result.finish_reason is not None else None),
        )

    def _notification(self, notification: Any) -> None:
        if self._trace is None:
            return
        payload = notification.payload if isinstance(notification.payload, dict) else {}
        self._trace.emit(
            "copilot",
            "session_notification",
            session_id=self.session_id,
            method=str(notification.method),
            status=str(payload.get("status", ""))[:64],
        )


class DeepSeekCopilotHarness:
    """Own one pinned DeepSeek runtime and its persistent coding sessions."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        workspace_root: Path,
        session_root: Path,
        base_url: str = ("https://generativelanguage.googleapis.com/v1beta/openai/"),
        timeout_seconds: float = 300.0,
        trace: DebugTrace | None = None,
        sdk_factory: Callable[..., Any] | None = None,
        installed_version: str | None = None,
        runtime_bin: Path | None = None,
        cordis_path: Path | None = None,
        docker: str | None = None,
        image: str = DEEPSEEK_RUNTIME_IMAGE,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self.workspace_root = workspace_root.resolve()
        self.session_root = session_root.resolve()
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.trace = trace
        self._sdk_factory = sdk_factory
        self._installed_version = installed_version
        self._runtime_bin = runtime_bin
        self._cordis_path = cordis_path
        self._docker = docker
        self._image = image
        self._sdk: Any | None = None
        self._sessions: dict[str, _DeepSeekSession] = {}

    @property
    def model(self) -> str:
        return self._model

    def require_ready(self) -> None:
        """Validate the optional dependency and start its isolated runtime."""
        if not self._api_key:
            raise DeepSeekHarnessUnavailable("the copilot API key is empty")
        version = self._installed_version or self._package_version()
        if version != DEEPSEEK_HARNESS_VERSION:
            raise DeepSeekHarnessUnavailable(
                "DeepSeek Harness SDK version mismatch: expected "
                f"{DEEPSEEK_HARNESS_VERSION}, found {version}"
            )
        docker = self._docker or shutil.which("docker")
        if docker is None:
            raise DeepSeekHarnessUnavailable("Docker is required for copilot isolation")
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._sdk is None:
            self._sdk = self._build_sdk(docker)
        try:
            self._sdk.start()
        except Exception as error:
            self.close()
            raise DeepSeekHarnessUnavailable(
                "the isolated DeepSeek Harness runtime failed its readiness check"
            ) from error
        if self.trace is not None:
            self.trace.emit(
                "copilot",
                "ready",
                adapter="deepseek-harness-sdk",
                version=version,
                model=self.model,
            )

    def session(self, session_id: str) -> CopilotSession:
        """Start or resume one named SDK session."""
        self.require_ready()
        if session_id not in self._sessions:
            raw_session = self._sdk.start_session(session_id)
            self._sessions[session_id] = _DeepSeekSession(raw_session, self.trace)
        return self._sessions[session_id]

    def close(self) -> None:
        """Stop the owned runtime without deleting durable session traces."""
        if self._sdk is not None:
            self._sdk.close()
        self._sdk = None
        self._sessions = {}

    def _build_sdk(self, docker: str) -> Any:
        factory = self._sdk_factory
        runtime_bin = self._runtime_bin
        cordis_path = self._cordis_path
        if factory is None:
            try:
                from deepseek_harness import DeepSeekHarness
                from deepseek_harness_runtime import resolve_bundled_launch_args
            except ImportError as error:
                raise DeepSeekHarnessUnavailable(
                    "install the pinned copilot dependency with pip install -e '.[copilot]'"
                ) from error
            launch = resolve_bundled_launch_args()
            if len(launch) != 1:
                raise DeepSeekHarnessUnavailable(
                    "the bundled DeepSeek Harness runtime is unsupported"
                )
            runtime_bin = Path(launch[0])
            cordis_path = runtime_bin.with_name("cordis.yml")
            factory = DeepSeekHarness
        if runtime_bin is None or cordis_path is None:
            raise DeepSeekHarnessUnavailable("DeepSeek Harness runtime files are unavailable")
        if not runtime_bin.is_file() or not cordis_path.is_file():
            raise DeepSeekHarnessUnavailable("DeepSeek Harness runtime files are unavailable")
        launcher = Path(__file__).with_name("launcher.py").resolve()
        return factory(
            provider="deepseek-official",
            model=self.model,
            cwd="/workspace",
            runtime_cwd="/",
            session_root="/sessions",
            cordis="/runtime/cordis.yml",
            api_key=self._api_key,
            base_url=self.base_url,
            request_timeout_seconds=self.timeout_seconds,
            launch_args_override=(
                sys.executable,
                str(launcher),
                "--docker",
                docker,
                "--image",
                self._image,
                "--runtime",
                str(runtime_bin.resolve()),
                "--cordis",
                str(cordis_path.resolve()),
                "--workspace",
                str(self.workspace_root),
                "--sessions",
                str(self.session_root),
            ),
        )

    @staticmethod
    def _package_version() -> str:
        try:
            return importlib.metadata.version("deepseek-harness-sdk")
        except importlib.metadata.PackageNotFoundError as error:
            raise DeepSeekHarnessUnavailable(
                "install the pinned copilot dependency with pip install -e '.[copilot]'"
            ) from error
