"""Tests for bounded Docker CLI subprocess handling."""

from pathlib import Path

import pytest

from execution.capsule.runtime import (
    DockerRuntime,
    DockerRuntimeOutputError,
    DockerRuntimeTimeoutError,
)


def fake_runtime(tmp_path: Path) -> DockerRuntime:
    binary = tmp_path / "fake-docker"
    binary.write_text(
        """#!/usr/bin/python3
import os
import sys
import time

mode = sys.argv[1]
if mode == "echo":
    sys.stdout.buffer.write(b"bounded-output")
elif mode == "large":
    sys.stdout.buffer.write(b"x" * 8192)
elif mode == "sleep":
    time.sleep(5)
elif mode == "environment":
    print("HOST_SECRET" in os.environ)
    print(os.environ.get("DOCKER_HOST", ""))
elif mode == "container":
    print("No such container", file=sys.stderr)
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return DockerRuntime(
        binary=str(binary),
        environ={
            "PATH": "/usr/bin:/bin",
            "DOCKER_HOST": "unix:///synthetic.sock",
            "HOST_SECRET": "must-not-be-inherited",
        },
    )


def test_runtime_collects_bounded_output_without_inheriting_host_secrets(
    tmp_path: Path,
) -> None:
    runtime = fake_runtime(tmp_path)

    output = runtime.run(
        ("echo",),
        timeout_seconds=1.0,
        output_limit_bytes=1024,
    )
    environment = runtime.run(
        ("environment",),
        timeout_seconds=1.0,
        output_limit_bytes=1024,
    )

    assert output.return_code == 0
    assert output.stdout == b"bounded-output"
    assert environment.stdout == b"False\nunix:///synthetic.sock\n"


def test_runtime_stops_collection_at_output_limit(tmp_path: Path) -> None:
    runtime = fake_runtime(tmp_path)

    with pytest.raises(DockerRuntimeOutputError, match="output"):
        runtime.run(
            ("large",),
            timeout_seconds=1.0,
            output_limit_bytes=1024,
        )


def test_runtime_terminates_client_at_timeout(tmp_path: Path) -> None:
    runtime = fake_runtime(tmp_path)

    with pytest.raises(DockerRuntimeTimeoutError, match="timeout"):
        runtime.run(
            ("sleep",),
            timeout_seconds=0.05,
            output_limit_bytes=1024,
        )


def test_cleanup_treats_absent_exact_container_as_already_removed(
    tmp_path: Path,
) -> None:
    runtime = fake_runtime(tmp_path)

    runtime.remove_container("exact-capsule", timeout_seconds=1.0)
