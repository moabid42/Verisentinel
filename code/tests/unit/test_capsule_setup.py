"""Tests for bounded local capsule setup."""

from pathlib import Path

import pytest

from execution.capsule.provider import CapsuleConfiguration
from execution.capsule.runtime import DockerRuntimeError, ProcessOutput
from execution.capsule.setup import CapsuleBuilder

IMAGE_ID = "sha256:" + "a" * 64


class ScriptedRuntime:
    """Return scripted Docker output and retain exact invocations."""

    def __init__(self, results: list[ProcessOutput]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput:
        del timeout_seconds, output_limit_bytes
        self.calls.append(arguments)
        return self.results.pop(0)


def output(return_code: int = 0, stdout: bytes = b"") -> ProcessOutput:
    return ProcessOutput(
        return_code=return_code,
        stdout=stdout,
        stderr=b"",
        duration_seconds=0.1,
    )


def configuration(tmp_path: Path) -> CapsuleConfiguration:
    return CapsuleConfiguration(
        image=f"verisentinel-capsule@{IMAGE_ID}",
        user_id=1000,
        group_id=1000,
        temporary_root=tmp_path,
    )


def test_build_verifies_image_and_creates_internal_network(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [output(), output(stdout=IMAGE_ID.encode()), output(1), output()]
    )

    report = CapsuleBuilder(configuration(tmp_path), runtime).build()

    assert report.network_created
    assert runtime.calls[0][:4] == (
        "build",
        "--network=none",
        "--pull=false",
        "--provenance=false",
    )
    assert runtime.calls[-1] == (
        "network",
        "create",
        "--internal",
        "verisentinel-capsule",
    )


def test_build_reuses_existing_internal_network(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [output(), output(stdout=IMAGE_ID.encode()), output(stdout=b"true\n")]
    )

    report = CapsuleBuilder(configuration(tmp_path), runtime).build()

    assert not report.network_created
    assert len(runtime.calls) == 3


def test_build_rejects_image_outside_digest_lock(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [output(), output(stdout=("sha256:" + "b" * 64).encode())]
    )

    with pytest.raises(DockerRuntimeError, match="digest lock"):
        CapsuleBuilder(configuration(tmp_path), runtime).build()


def test_build_rejects_existing_external_network(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        [output(), output(stdout=IMAGE_ID.encode()), output(stdout=b"false\n")]
    )

    with pytest.raises(DockerRuntimeError, match="not internal"):
        CapsuleBuilder(configuration(tmp_path), runtime).build()
