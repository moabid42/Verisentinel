"""Bounded subprocess boundary for the local Docker runtime."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

_DOCKER_ENVIRONMENT_KEYS = (
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


class DockerRuntimeError(RuntimeError):
    """The local container runtime could not complete a bounded operation."""


class DockerRuntimeUnavailableError(DockerRuntimeError):
    """The Docker client or daemon is unavailable."""


class DockerRuntimeTimeoutError(DockerRuntimeError):
    """A Docker operation exceeded its fixed deadline."""


class DockerRuntimeOutputError(DockerRuntimeError):
    """A Docker operation exceeded its fixed output limit."""


class DockerRuntimeCleanupError(DockerRuntimeError):
    """A Docker container could not be removed."""


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """Bounded result of one Docker CLI operation."""

    return_code: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float


class DockerRuntime:
    """Invoke Docker without a shell and capture output incrementally."""

    def __init__(
        self,
        binary: str = "docker",
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.binary = binary
        source = environ if environ is not None else os.environ
        self.environment = {"PATH": source.get("PATH", "/usr/bin:/bin")}
        self.environment.update(
            (key, source[key]) for key in _DOCKER_ENVIRONMENT_KEYS if key in source
        )

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput:
        """Run one bounded Docker command with no inherited standard input."""
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                (self.binary, *arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise DockerRuntimeUnavailableError(
                "local container runtime is unavailable"
            ) from None
        try:
            stdout, stderr = self._collect(
                process,
                timeout_seconds=timeout_seconds,
                output_limit_bytes=output_limit_bytes,
            )
            return ProcessOutput(
                return_code=process.wait(),
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
            )
        except (DockerRuntimeTimeoutError, DockerRuntimeOutputError):
            self._terminate(process)
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def remove_container(self, name: str, *, timeout_seconds: float = 10.0) -> None:
        """Remove one exact container and its anonymous volumes."""
        result = self.run(
            ("container", "rm", "--force", "--volumes", name),
            timeout_seconds=timeout_seconds,
            output_limit_bytes=16_384,
        )
        if result.return_code == 0 or b"No such container" in result.stderr:
            return
        raise DockerRuntimeCleanupError("capsule container cleanup failed")

    @staticmethod
    def _collect(
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> tuple[bytes, bytes]:
        if process.stdout is None or process.stderr is None:
            raise DockerRuntimeUnavailableError(
                "local container runtime output is unavailable"
            )
        deadline = time.monotonic() + timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        total = 0
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerRuntimeTimeoutError(
                        "capsule execution exceeded its timeout"
                    )
                events = selector.select(min(remaining, 0.1))
                if not events and process.poll() is not None:
                    events = selector.select(0)
                for key, _ in events:
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > output_limit_bytes:
                        raise DockerRuntimeOutputError(
                            "capsule output exceeded its configured limit"
                        )
                    buffers[key.data].extend(chunk)
            return bytes(buffers["stdout"]), bytes(buffers["stderr"])
        finally:
            selector.close()

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)
