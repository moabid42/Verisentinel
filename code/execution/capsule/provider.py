"""Ephemeral local-container execution provider."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from core.models import ExecutionObservation, ExecutionSpec
from execution.capsule.runtime import (
    DockerRuntime,
    DockerRuntimeOutputError,
    DockerRuntimeTimeoutError,
    DockerRuntimeUnavailableError,
    ProcessOutput,
)
from execution.credentials import CredentialLease

_CAPSULE_DIRECTORY = Path(__file__).resolve().parent
_IMAGE_LOCK_PATH = _CAPSULE_DIRECTORY / "image.lock"
_IMAGE_PATTERN = re.compile(r"^verisentinel-capsule@sha256:[0-9a-f]{64}$")
_NETWORK_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_SPEC_DESTINATION = "/run/verisentinel/spec.json"
_CREDENTIAL_DESTINATION = "/run/verisentinel/credential"


class CapsuleExecutionError(RuntimeError):
    """A bounded, non-sensitive local capsule failure."""


class CapsuleRuntime(Protocol):
    """Docker operations required by the capsule provider."""

    def run_container(
        self,
        arguments: tuple[str, ...],
        *,
        container_name: str,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput: ...

    def remove_container(
        self,
        name: str,
        *,
        timeout_seconds: float,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CapsuleLimits:
    """Fixed local capsule resource limits."""

    cpu_count: float = 0.5
    memory_bytes: int = 134_217_728
    process_count: int = 32
    temporary_storage_bytes: int = 16_777_216
    docker_output_overhead_bytes: int = 4096
    cleanup_timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class CapsuleConfiguration:
    """Validated immutable local capsule configuration."""

    image: str
    network: str = "verisentinel-capsule"
    user_id: int = field(default_factory=os.getuid)
    group_id: int = field(default_factory=os.getgid)
    temporary_root: Path | None = None
    limits: CapsuleLimits = field(default_factory=CapsuleLimits)

    def __post_init__(self) -> None:
        if _IMAGE_PATTERN.fullmatch(self.image) is None:
            raise ValueError("capsule image must use the repository digest lock")
        if _NETWORK_PATTERN.fullmatch(self.network) is None:
            raise ValueError("capsule network name has an invalid format")
        if self.user_id <= 0 or self.group_id < 0:
            raise ValueError("capsule runtime requires a non-root user")

    @classmethod
    def from_environment(cls) -> CapsuleConfiguration:
        """Load the digest lock and non-sensitive network selection."""
        try:
            image = _IMAGE_LOCK_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            raise ValueError("capsule image digest lock is unavailable") from None
        return cls(
            image=image,
            network=os.getenv("VERISENTINEL_CAPSULE_NETWORK", "verisentinel-capsule"),
        )


@dataclass(frozen=True, slots=True)
class CapsuleMetrics:
    """Non-sensitive timing from the most recent provider invocation."""

    startup_latency_seconds: float | None
    total_duration_seconds: float
    peak_memory_bytes: int | None
    cleanup_duration_seconds: float


class CapsuleExecutionProvider:
    """Run one approved specification in a bounded ephemeral container."""

    name: Literal["capsule"] = "capsule"

    def __init__(
        self,
        configuration: CapsuleConfiguration | None = None,
        runtime: CapsuleRuntime | None = None,
    ) -> None:
        self.configuration = configuration or CapsuleConfiguration.from_environment()
        self.runtime = runtime or DockerRuntime()
        self.last_metrics: CapsuleMetrics | None = None

    def execute(
        self,
        spec: ExecutionSpec,
        credential_lease: CredentialLease,
    ) -> ExecutionObservation:
        """Execute one spec with fixed isolation flags and guaranteed cleanup."""
        container_name = f"verisentinel-capsule-{secrets.token_hex(8)}"
        prepared = _PreparedInputs.create(
            spec,
            credential_lease.access_token,
            temporary_root=self.configuration.temporary_root,
        )
        result: ProcessOutput | None = None
        primary_error: CapsuleExecutionError | None = None
        cleanup_started = 0.0
        cleanup_duration = 0.0
        try:
            arguments = self._docker_arguments(container_name, prepared)
            try:
                result = self.runtime.run_container(
                    arguments,
                    container_name=container_name,
                    timeout_seconds=spec.timeout_seconds,
                    output_limit_bytes=(
                        spec.output_limit_bytes
                        + self.configuration.limits.docker_output_overhead_bytes
                    ),
                )
                observation = self._observation(result)
            except DockerRuntimeTimeoutError:
                primary_error = CapsuleExecutionError(
                    "capsule execution exceeded its timeout"
                )
            except DockerRuntimeOutputError:
                primary_error = CapsuleExecutionError(
                    "capsule output exceeded its configured limit"
                )
            except DockerRuntimeUnavailableError:
                primary_error = CapsuleExecutionError(
                    "local container runtime is unavailable"
                )
            except CapsuleExecutionError as error:
                primary_error = error
            except Exception:
                primary_error = CapsuleExecutionError("capsule process failed")
        finally:
            cleanup_started = time.monotonic()
            cleanup_error = self._cleanup(container_name, prepared)
            cleanup_duration = time.monotonic() - cleanup_started
        self.last_metrics = CapsuleMetrics(
            startup_latency_seconds=(
                result.startup_latency_seconds if result is not None else None
            ),
            total_duration_seconds=(result.duration_seconds if result is not None else 0.0),
            peak_memory_bytes=(
                result.peak_memory_bytes if result is not None else None
            ),
            cleanup_duration_seconds=cleanup_duration,
        )
        if cleanup_error is not None:
            raise cleanup_error
        if primary_error is not None:
            raise primary_error
        return observation

    def _docker_arguments(
        self,
        container_name: str,
        prepared: _PreparedInputs,
    ) -> tuple[str, ...]:
        limits = self.configuration.limits
        memory = str(limits.memory_bytes)
        spec_mount = _mount_argument(prepared.spec_path, _SPEC_DESTINATION)
        credential_mount = _mount_argument(
            prepared.credential_path,
            _CREDENTIAL_DESTINATION,
        )
        return (
            "run",
            "--name",
            container_name,
            "--pull",
            "never",
            "--network",
            self.configuration.network,
            "--read-only",
            "--user",
            f"{self.configuration.user_id}:{self.configuration.group_id}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(limits.process_count),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            str(limits.cpu_count),
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,"
                f"size={limits.temporary_storage_bytes},mode=1777"
            ),
            "--ulimit",
            "nofile=64:64",
            "--mount",
            spec_mount,
            "--mount",
            credential_mount,
            self.configuration.image,
        )

    @staticmethod
    def _observation(result: ProcessOutput) -> ExecutionObservation:
        if result.return_code != 0:
            error = _translated_process_error(result.stderr)
            raise CapsuleExecutionError(error)
        if result.stderr:
            raise CapsuleExecutionError(
                "capsule returned unexpected diagnostic output"
            )
        try:
            return ExecutionObservation.model_validate_json(result.stdout)
        except (ValidationError, ValueError):
            raise CapsuleExecutionError("capsule returned malformed output") from None

    def _cleanup(
        self,
        container_name: str,
        prepared: _PreparedInputs,
    ) -> CapsuleExecutionError | None:
        failed = False
        try:
            self.runtime.remove_container(
                container_name,
                timeout_seconds=(
                    self.configuration.limits.cleanup_timeout_seconds
                ),
            )
        except Exception:
            failed = True
        try:
            prepared.cleanup()
        except OSError:
            failed = True
        if failed:
            return CapsuleExecutionError("capsule cleanup failed")
        return None


@dataclass(slots=True)
class _PreparedInputs:
    directory: Path
    spec_path: Path
    credential_path: Path

    @classmethod
    def create(
        cls,
        spec: ExecutionSpec,
        credential: str,
        *,
        temporary_root: Path | None,
    ) -> _PreparedInputs:
        try:
            directory = Path(
                tempfile.mkdtemp(
                    prefix="verisentinel-capsule-",
                    dir=temporary_root,
                )
            )
            directory.chmod(0o700)
            spec_path = directory / "spec.json"
            credential_path = directory / "credential"
            spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
            credential_path.write_text(credential, encoding="utf-8")
            spec_path.chmod(0o400)
            credential_path.chmod(0o400)
        except OSError:
            if "directory" in locals():
                shutil.rmtree(directory, ignore_errors=True)
            raise CapsuleExecutionError("capsule input preparation failed") from None
        return cls(
            directory=directory,
            spec_path=spec_path,
            credential_path=credential_path,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.directory)


def _mount_argument(source: Path, destination: str) -> str:
    rendered = str(source.resolve())
    if "," in rendered:
        raise CapsuleExecutionError("capsule input path is unsupported")
    return f"type=bind,src={rendered},dst={destination},readonly"


def _translated_process_error(stderr: bytes) -> str:
    code = stderr.strip()
    messages = {
        b"capsule_error:credential_unavailable": (
            "capsule credential file is unavailable"
        ),
        b"capsule_error:endpoint_failed": "capsule endpoint failed",
        b"capsule_error:invalid_parameters": "capsule rejected action parameters",
        b"capsule_error:invalid_spec": "capsule rejected its execution specification",
        b"capsule_error:malformed_output": "capsule endpoint returned malformed output",
        b"capsule_error:output_exceeded": (
            "capsule output exceeded its configured limit"
        ),
        b"capsule_error:unregistered_operation": (
            "capsule rejected an unregistered operation"
        ),
    }
    return messages.get(code, "capsule process failed")
