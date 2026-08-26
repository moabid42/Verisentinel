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
from datetime import datetime
from pathlib import Path

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
    startup_latency_seconds: float | None = None
    peak_memory_bytes: int | None = None


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
        return self._run(
            arguments,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
            container_name=None,
        )

    def run_container(
        self,
        arguments: Sequence[str],
        *,
        container_name: str,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput:
        """Run one container while measuring startup and peak memory."""
        return self._run(
            arguments,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
            container_name=container_name,
        )

    def _run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        container_name: str | None,
    ) -> ProcessOutput:
        started = time.monotonic()
        started_wall = time.time()
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
            monitor = (
                _ContainerMonitor(
                    binary=self.binary,
                    environment=self.environment,
                    container_name=container_name,
                    command_started_at=started_wall,
                )
                if container_name is not None
                else None
            )
            stdout, stderr = self._collect(
                process,
                timeout_seconds=timeout_seconds,
                output_limit_bytes=output_limit_bytes,
                monitor=monitor,
            )
            return ProcessOutput(
                return_code=process.wait(),
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
                startup_latency_seconds=(
                    monitor.startup_latency_seconds if monitor is not None else None
                ),
                peak_memory_bytes=(
                    monitor.peak_memory_bytes if monitor is not None else None
                ),
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
        monitor: _ContainerMonitor | None,
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
                if monitor is not None:
                    monitor.poll()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerRuntimeTimeoutError(
                        "capsule execution exceeded its timeout"
                    )
                interval = 0.05 if monitor is not None else 0.1
                events = selector.select(min(remaining, interval))
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
            if monitor is not None:
                monitor.poll(force=True)
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


@dataclass(slots=True)
class _ContainerMonitor:
    binary: str
    environment: Mapping[str, str]
    container_name: str
    command_started_at: float
    startup_latency_seconds: float | None = None
    peak_memory_bytes: int | None = None
    _process_id: int | None = None
    _memory_peak_path: Path | None = None
    _next_poll: float = 0.0

    def poll(self, *, force: bool = False) -> None:
        """Collect bounded local runtime metrics without container requests."""
        now = time.monotonic()
        if not force and now < self._next_poll:
            return
        self._next_poll = now + 0.05
        if self._process_id is None:
            self._inspect_container()
        if self._memory_peak_path is not None:
            self._read_memory_peak()

    def _inspect_container(self) -> None:
        try:
            result = subprocess.run(
                (
                    self.binary,
                    "container",
                    "inspect",
                    self.container_name,
                    "--format",
                    "{{.State.Pid}}|{{.State.StartedAt}}",
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.environment,
                timeout=0.5,
                check=False,
            )
            if result.returncode != 0 or len(result.stdout) > 4096:
                return
            process_id, started_at = result.stdout.decode("utf-8").strip().split(
                "|",
                maxsplit=1,
            )
            parsed_process_id = int(process_id)
            if parsed_process_id <= 0:
                return
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            self.startup_latency_seconds = max(
                0.0,
                started.timestamp() - self.command_started_at,
            )
            self._process_id = parsed_process_id
            self._memory_peak_path = _memory_peak_path(parsed_process_id)
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
            return

    def _read_memory_peak(self) -> None:
        try:
            rendered = self._memory_peak_path.read_text(encoding="utf-8").strip()
            current = int(rendered)
        except (OSError, ValueError):
            return
        self.peak_memory_bytes = max(self.peak_memory_bytes or 0, current)


def _memory_peak_path(process_id: int) -> Path | None:
    try:
        lines = Path(f"/proc/{process_id}/cgroup").read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split(":", maxsplit=2)
        if len(fields) == 3 and fields[0] == "0":
            return Path("/sys/fs/cgroup") / fields[2].lstrip("/") / "memory.peak"
    return None
