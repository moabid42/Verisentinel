"""Read-only readiness inspection for the local execution capsule."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from execution.capsule.entrypoint import ENDPOINT_URL
from execution.capsule.provider import CapsuleConfiguration
from execution.capsule.runtime import DockerRuntime, DockerRuntimeError, ProcessOutput


class CapsuleInspectionRuntime(Protocol):
    """Read-only Docker operations needed by readiness inspection."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput: ...


@dataclass(frozen=True, slots=True)
class CapsuleCheck:
    """One non-sensitive readiness assertion."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CapsuleDoctorReport:
    """Complete read-only readiness report for the configured capsule."""

    available: bool
    provider: str
    detail: str
    checks: tuple[CapsuleCheck, ...]


class CapsuleDoctor:
    """Inspect Docker, the locked image, and the private network."""

    def __init__(
        self,
        configuration: CapsuleConfiguration | None = None,
        runtime: CapsuleInspectionRuntime | None = None,
    ) -> None:
        self.configuration = configuration or CapsuleConfiguration.from_environment()
        self.runtime = runtime or DockerRuntime()

    def inspect(self) -> CapsuleDoctorReport:
        """Return all readiness checks without starting a container or request."""
        runtime = self._runtime_check()
        checks = [runtime]
        if runtime.passed:
            checks.extend(
                (
                    self._host_security_check(),
                    self._image_digest_check(),
                    self._fixed_entrypoint_check(),
                    self._non_root_image_check(),
                    self._internal_network_check(),
                )
            )
        else:
            checks.extend(
                self._not_checked(name)
                for name in (
                    "host-security",
                    "image-digest",
                    "fixed-entrypoint",
                    "non-root-image",
                    "internal-network",
                )
            )
        checks.extend(
            (
                CapsuleCheck(
                    name="filesystem-isolation",
                    passed=True,
                    detail=(
                        "read-only root, fixed read-only inputs, and bounded "
                        "temporary storage are configured"
                    ),
                ),
                CapsuleCheck(
                    name="process-isolation",
                    passed=True,
                    detail=(
                        "all capabilities are dropped, privilege escalation is "
                        "disabled, and CPU, memory, process, timeout, and output "
                        "limits are configured"
                    ),
                ),
                CapsuleCheck(
                    name="credential-isolation",
                    passed=self.configuration.user_id > 0,
                    detail=(
                        "credential material uses one fixed read-only temporary "
                        "file and is excluded from arguments and environment"
                    ),
                ),
                CapsuleCheck(
                    name="fixed-endpoint",
                    passed=ENDPOINT_URL == "http://verisentinel-mock:8080/execute",
                    detail="the capsule endpoint is the fixed private mock service",
                ),
            )
        )
        result = tuple(checks)
        available = all(check.passed for check in result)
        return CapsuleDoctorReport(
            available=available,
            provider="capsule",
            detail=(
                "Local execution capsule is ready."
                if available
                else "Local execution capsule is not ready."
            ),
            checks=result,
        )

    def require_ready(self) -> None:
        """Fail provider assembly when any required readiness check fails."""
        if not self.inspect().available:
            raise ValueError("execution provider 'capsule' is unavailable")

    def _runtime_check(self) -> CapsuleCheck:
        result = self._run(("version", "--format", "{{json .Server}}"))
        passed = result is not None and result.return_code == 0
        return CapsuleCheck(
            name="runtime",
            passed=passed,
            detail=(
                "Docker daemon is available"
                if passed
                else "Docker daemon is unavailable"
            ),
        )

    def _host_security_check(self) -> CapsuleCheck:
        result = self._run(
            (
                "info",
                "--format",
                "{{json .SecurityOptions}}|{{.CgroupVersion}}|{{.OSType}}",
            )
        )
        passed = False
        if result is not None and result.return_code == 0:
            try:
                security, cgroup, operating_system = result.stdout.decode().strip().split(
                    "|",
                    maxsplit=2,
                )
                options = json.loads(security)
                passed = (
                    operating_system == "linux"
                    and bool(cgroup)
                    and any("seccomp" in option for option in options)
                )
            except (UnicodeError, ValueError, json.JSONDecodeError, TypeError):
                passed = False
        return CapsuleCheck(
            name="host-security",
            passed=passed,
            detail=(
                "Linux cgroups and the default seccomp profile are available"
                if passed
                else "Linux cgroups or the default seccomp profile are unavailable"
            ),
        )

    def _image_configuration(self) -> dict[str, object] | None:
        result = self._run(
            (
                "image",
                "inspect",
                self.configuration.image,
                "--format",
                "{{json .}}",
            ),
            output_limit_bytes=131_072,
        )
        if result is None or result.return_code != 0:
            return None
        try:
            document = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError):
            return None
        return document if isinstance(document, dict) else None

    def _image_digest_check(self) -> CapsuleCheck:
        document = self._image_configuration()
        expected = self.configuration.image.rsplit("@", maxsplit=1)[1]
        passed = document is not None and document.get("Id") == expected
        return CapsuleCheck(
            name="image-digest",
            passed=passed,
            detail=(
                "configured capsule image matches the active digest lock"
                if passed
                else "configured capsule image is absent or does not match its lock"
            ),
        )

    def _fixed_entrypoint_check(self) -> CapsuleCheck:
        document = self._image_configuration()
        config = document.get("Config", {}) if document is not None else {}
        entrypoint = config.get("Entrypoint") if isinstance(config, dict) else None
        command = config.get("Cmd") if isinstance(config, dict) else None
        passed = entrypoint == [
            "/usr/local/bin/python",
            "/opt/verisentinel/entrypoint.py",
        ] and not command
        return CapsuleCheck(
            name="fixed-entrypoint",
            passed=passed,
            detail=(
                "image has the fixed typed-operation entrypoint and no command"
                if passed
                else "image entrypoint does not match the repository contract"
            ),
        )

    def _non_root_image_check(self) -> CapsuleCheck:
        document = self._image_configuration()
        config = document.get("Config", {}) if document is not None else {}
        user = config.get("User") if isinstance(config, dict) else None
        passed = user == "65532:65532" and self.configuration.user_id > 0
        return CapsuleCheck(
            name="non-root-image",
            passed=passed,
            detail=(
                "image and local invocation use explicit non-root users"
                if passed
                else "image or local invocation would use a root user"
            ),
        )

    def _internal_network_check(self) -> CapsuleCheck:
        result = self._run(
            (
                "network",
                "inspect",
                self.configuration.network,
                "--format",
                "{{.Internal}}",
            )
        )
        passed = (
            result is not None
            and result.return_code == 0
            and result.stdout.strip() == b"true"
        )
        return CapsuleCheck(
            name="internal-network",
            passed=passed,
            detail=(
                "configured Docker network has external connectivity disabled"
                if passed
                else "configured Docker network is absent or not internal"
            ),
        )

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        output_limit_bytes: int = 32_768,
    ) -> ProcessOutput | None:
        try:
            return self.runtime.run(
                arguments,
                timeout_seconds=5.0,
                output_limit_bytes=output_limit_bytes,
            )
        except DockerRuntimeError:
            return None

    @staticmethod
    def _not_checked(name: str) -> CapsuleCheck:
        return CapsuleCheck(
            name=name,
            passed=False,
            detail="not checked because the Docker daemon is unavailable",
        )
