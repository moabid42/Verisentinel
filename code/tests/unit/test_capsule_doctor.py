"""Tests for read-only capsule readiness inspection."""

import json
from pathlib import Path

import pytest

from execution.capsule.doctor import CapsuleDoctor
from execution.capsule.provider import CapsuleConfiguration
from execution.capsule.runtime import (
    DockerRuntimeUnavailableError,
    ProcessOutput,
)

IMAGE = (
    "verisentinel-capsule@sha256:d4670ddecec6eb9df14b990c03aa258dece2078f8114401c7f2086e4c4089ffe"
)
GATEWAY_IMAGE = "verisentinel-gateway@sha256:" + "a" * 64


class InspectionRuntime:
    """Return deterministic Docker inspection output by operation."""

    def __init__(self, *, network_internal: bool = True) -> None:
        self.network_internal = network_internal
        self.arguments: list[tuple[str, ...]] = []
        self.unavailable = False

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput:
        del timeout_seconds, output_limit_bytes
        self.arguments.append(arguments)
        if self.unavailable:
            raise DockerRuntimeUnavailableError("untrusted runtime detail")
        if arguments[0] == "version":
            stdout = b'{"Version":"synthetic"}\n'
        elif arguments[0] == "info":
            stdout = b'["name=seccomp,profile=builtin"]|2|linux\n'
        elif arguments[0] == "image":
            if arguments[2].startswith("verisentinel-gateway@"):
                stdout = GATEWAY_IMAGE.rsplit("@", maxsplit=1)[1].encode()
            else:
                stdout = json.dumps(
                    {
                        "Id": IMAGE.rsplit("@", maxsplit=1)[1],
                        "Config": {
                            "User": "65532:65532",
                            "Entrypoint": [
                                "/usr/local/bin/python",
                                "/opt/verisentinel/entrypoint.py",
                            ],
                        },
                    }
                ).encode()
        else:
            stdout = b"true\n" if self.network_internal else b"false\n"
        return ProcessOutput(
            return_code=0,
            stdout=stdout,
            stderr=b"",
            duration_seconds=0.01,
        )


def doctor(runtime: InspectionRuntime, tmp_path: Path) -> CapsuleDoctor:
    gateway_lock = tmp_path / "gateway.lock"
    gateway_lock.write_text(GATEWAY_IMAGE, encoding="utf-8")
    return CapsuleDoctor(
        configuration=CapsuleConfiguration(
            image=IMAGE,
            network="private-mock-network",
            user_id=1000,
            group_id=1000,
        ),
        runtime=runtime,
        gateway_lock_path=gateway_lock,
    )


def test_doctor_reports_every_required_capability_ready(tmp_path: Path) -> None:
    runtime = InspectionRuntime()

    report = doctor(runtime, tmp_path).inspect()

    assert report.available
    assert report.provider == "capsule"
    assert {check.name for check in report.checks} == {
        "runtime",
        "host-security",
        "image-digest",
        "gateway-image",
        "fixed-entrypoint",
        "non-root-image",
        "internal-network",
        "filesystem-isolation",
        "process-isolation",
        "credential-isolation",
        "fixed-endpoint",
    }
    assert all(check.passed for check in report.checks)
    assert not any("request" in arguments for arguments in runtime.arguments)


def test_doctor_reports_internal_network_failure(tmp_path: Path) -> None:
    report = doctor(InspectionRuntime(network_internal=False), tmp_path).inspect()

    assert not report.available
    failed = {check.name for check in report.checks if not check.passed}
    assert failed == {"internal-network"}


def test_doctor_stops_runtime_inspection_when_daemon_is_unavailable(
    tmp_path: Path,
) -> None:
    runtime = InspectionRuntime()
    runtime.unavailable = True

    report = doctor(runtime, tmp_path).inspect()

    assert not report.available
    assert len(runtime.arguments) == 1
    assert report.checks[0].name == "runtime"
    assert all(not check.passed for check in report.checks[:7])


def test_doctor_rejects_unavailable_provider_assembly(tmp_path: Path) -> None:
    runtime = InspectionRuntime(network_internal=False)

    with pytest.raises(ValueError, match="unavailable"):
        doctor(runtime, tmp_path).require_ready()
