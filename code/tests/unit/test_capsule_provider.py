"""Tests for bounded local-capsule provider execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.models import (
    ActionDefinition,
    ExecutionObservation,
    ExecutionSpec,
    TechniqueActionParameters,
)
from execution.capsule.provider import (
    CapsuleConfiguration,
    CapsuleExecutionError,
    CapsuleExecutionProvider,
)
from execution.capsule.runtime import (
    DockerRuntimeCleanupError,
    DockerRuntimeOutputError,
    DockerRuntimeTimeoutError,
    ProcessOutput,
)
from execution.credentials import CredentialLease, CredentialSourceKind

ACCESS_TOKEN = "synthetic-capsule-provider-token"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
IMAGE = (
    "verisentinel-capsule@sha256:"
    "d4670ddecec6eb9df14b990c03aa258dece2078f8114401c7f2086e4c4089ffe"
)


class RecordingRuntime:
    """Record Docker operations and inspect prepared files during execution."""

    def __init__(self, result: ProcessOutput) -> None:
        self.result = result
        self.run_error: Exception | None = None
        self.cleanup_error: Exception | None = None
        self.arguments: tuple[str, ...] = ()
        self.container_name = ""
        self.spec_path: Path | None = None
        self.credential_path: Path | None = None
        self.cleanup_timeout = 0.0

    def run_container(
        self,
        arguments: tuple[str, ...],
        *,
        container_name: str,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput:
        del timeout_seconds, output_limit_bytes
        self.arguments = arguments
        self.container_name = arguments[arguments.index("--name") + 1]
        assert container_name == self.container_name
        mounts = [
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "--mount"
        ]
        self.spec_path = _mount_source(mounts[0])
        self.credential_path = _mount_source(mounts[1])
        assert self.spec_path.stat().st_mode & 0o777 == 0o400
        assert self.credential_path.stat().st_mode & 0o777 == 0o400
        assert self.credential_path.read_text(encoding="utf-8") == ACCESS_TOKEN
        assert ACCESS_TOKEN not in self.spec_path.read_text(encoding="utf-8")
        if self.run_error is not None:
            raise self.run_error
        return self.result

    def remove_container(
        self,
        name: str,
        *,
        timeout_seconds: float,
    ) -> None:
        assert name == self.container_name
        self.cleanup_timeout = timeout_seconds
        if self.cleanup_error is not None:
            raise self.cleanup_error


def execution_spec() -> ExecutionSpec:
    return ExecutionSpec(
        engagement_id="engagement",
        candidate_id="candidate",
        action=ActionDefinition(
            action_id="technique:known",
            technique_id="known",
            observed_permission_footprint=("storage.objects.get",),
            expected_capabilities=("capability",),
        ),
        identity="runner@example.test",
        target="projects/authorized",
        arguments=TechniqueActionParameters(),
        validator_result_id="validation",
        approval_id="approval",
        state_version="state",
        matrix_version="matrix",
        credential_ref="run/default",
        timeout_seconds=5.0,
        output_limit_bytes=4096,
    )


def observation(spec: ExecutionSpec | None = None) -> ExecutionObservation:
    spec = spec or execution_spec()
    return ExecutionObservation(
        execution_id="execution",
        engagement_id=spec.engagement_id,
        action_id=spec.action.action_id,
        identity=spec.identity,
        target=spec.target,
        success=True,
        api_response_summary="mock endpoint accepted typed operation",
        gained_capabilities=spec.action.expected_capabilities,
        observed_permission_footprint=(
            spec.action.observed_permission_footprint
        ),
    )


def lease() -> CredentialLease:
    return CredentialLease(
        principal="runner@example.test",
        expires_at=NOW + timedelta(minutes=10),
        source_kind=CredentialSourceKind.ENV,
        _access_token=ACCESS_TOKEN,
    )


def process_output(
    *,
    return_code: int = 0,
    stdout: bytes | None = None,
    stderr: bytes = b"",
) -> ProcessOutput:
    return ProcessOutput(
        return_code=return_code,
        stdout=stdout or observation().model_dump_json().encode("utf-8"),
        stderr=stderr,
        duration_seconds=0.25,
        startup_latency_seconds=0.05,
        peak_memory_bytes=12_345_678,
    )


def provider(
    tmp_path: Path,
    runtime: RecordingRuntime,
) -> CapsuleExecutionProvider:
    return CapsuleExecutionProvider(
        configuration=CapsuleConfiguration(
            image=IMAGE,
            network="private-mock-network",
            user_id=1000,
            group_id=1000,
            temporary_root=tmp_path,
        ),
        runtime=runtime,
    )


def test_provider_applies_all_isolation_and_resource_bounds(tmp_path: Path) -> None:
    runtime = RecordingRuntime(process_output())
    capsule = provider(tmp_path, runtime)

    result = capsule.execute(execution_spec(), lease())

    assert result.execution_id == "execution"
    assert result.engagement_id == "engagement"
    assert result.api_response_summary == "mock endpoint accepted typed operation"
    arguments = runtime.arguments
    assert arguments[0] == "run"
    assert arguments[-1] == IMAGE
    assert "--entrypoint" not in arguments
    assert "--read-only" in arguments
    assert _option(arguments, "--network") == "private-mock-network"
    assert _option(arguments, "--user") == "1000:1000"
    assert _option(arguments, "--cap-drop") == "ALL"
    assert _option(arguments, "--security-opt") == "no-new-privileges=true"
    assert _option(arguments, "--pids-limit") == "32"
    assert _option(arguments, "--memory") == "134217728"
    assert _option(arguments, "--memory-swap") == "134217728"
    assert _option(arguments, "--cpus") == "0.5"
    assert _option(arguments, "--ulimit") == "nofile=64:64"
    assert "size=16777216" in _option(arguments, "--tmpfs")
    assert "noexec" in _option(arguments, "--tmpfs")
    rendered = " ".join(arguments)
    assert ACCESS_TOKEN not in rendered
    assert "GEMINI_API_KEY" not in rendered
    assert str(Path(__file__).parents[3]) not in rendered
    assert runtime.cleanup_timeout == 10.0
    assert capsule.last_metrics is not None
    assert capsule.last_metrics.startup_latency_seconds == 0.05
    assert capsule.last_metrics.total_duration_seconds == 0.25
    assert capsule.last_metrics.peak_memory_bytes == 12_345_678


def test_provider_removes_temporary_inputs_after_success(tmp_path: Path) -> None:
    runtime = RecordingRuntime(process_output())
    capsule = provider(tmp_path, runtime)

    capsule.execute(execution_spec(), lease())

    assert runtime.spec_path is not None
    assert runtime.credential_path is not None
    assert not runtime.spec_path.exists()
    assert not runtime.credential_path.exists()
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize(
    ("runtime_error", "message"),
    [
        (
            DockerRuntimeTimeoutError("unbounded detail"),
            "capsule execution exceeded its timeout",
        ),
        (
            DockerRuntimeOutputError("unbounded detail"),
            "capsule output exceeded its configured limit",
        ),
    ],
)
def test_provider_translates_runtime_bounds_and_cleans_up(
    tmp_path: Path,
    runtime_error: Exception,
    message: str,
) -> None:
    runtime = RecordingRuntime(process_output())
    runtime.run_error = runtime_error
    capsule = provider(tmp_path, runtime)

    with pytest.raises(CapsuleExecutionError, match=message):
        capsule.execute(execution_spec(), lease())

    assert runtime.spec_path is not None
    assert not runtime.spec_path.exists()
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            process_output(
                return_code=1,
                stderr=b"capsule_error:endpoint_failed\n",
            ),
            "capsule endpoint failed",
        ),
        (
            process_output(return_code=1, stderr=b"untrusted provider detail"),
            "capsule process failed",
        ),
        (
            process_output(stdout=b"not-json"),
            "capsule returned malformed output",
        ),
        (
            process_output(stderr=b"unexpected diagnostic"),
            "capsule returned unexpected diagnostic output",
        ),
    ],
)
def test_provider_translates_process_failures_without_details(
    tmp_path: Path,
    result: ProcessOutput,
    message: str,
) -> None:
    runtime = RecordingRuntime(result)
    capsule = provider(tmp_path, runtime)

    with pytest.raises(CapsuleExecutionError, match=message) as raised:
        capsule.execute(execution_spec(), lease())

    assert "untrusted provider detail" not in str(raised.value)
    assert tuple(tmp_path.iterdir()) == ()


def test_cleanup_failure_overrides_success_and_removes_host_files(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(process_output())
    runtime.cleanup_error = DockerRuntimeCleanupError("untrusted detail")
    capsule = provider(tmp_path, runtime)

    with pytest.raises(CapsuleExecutionError, match="capsule cleanup failed"):
        capsule.execute(execution_spec(), lease())

    assert tuple(tmp_path.iterdir()) == ()


def test_interruption_removes_temporary_inputs_before_propagating(
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime(process_output())
    runtime.run_error = KeyboardInterrupt()
    capsule = provider(tmp_path, runtime)

    with pytest.raises(KeyboardInterrupt):
        capsule.execute(execution_spec(), lease())

    assert runtime.spec_path is not None
    assert not runtime.spec_path.exists()
    assert tuple(tmp_path.iterdir()) == ()


def test_configuration_rejects_unpinned_image_or_root_user() -> None:
    with pytest.raises(ValueError, match="digest reference"):
        CapsuleConfiguration(image="verisentinel-capsule:latest")
    with pytest.raises(ValueError, match="non-root"):
        CapsuleConfiguration(image=IMAGE, user_id=0)


def test_configuration_prefers_active_local_image_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active_image = "verisentinel-capsule@sha256:" + "b" * 64
    lock_path = tmp_path / "capsule" / "image.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(active_image, encoding="utf-8")
    monkeypatch.setenv("IAM_PLANNER_RUNTIME", str(tmp_path))

    configuration = CapsuleConfiguration.from_environment()

    assert configuration.image == active_image


def _mount_source(value: str) -> Path:
    parts = value.split(",")
    assert "readonly" in parts
    fields = dict(
        field.split("=", maxsplit=1) for field in parts if "=" in field
    )
    return Path(fields["src"])


def _option(arguments: tuple[str, ...], name: str) -> str:
    return arguments[arguments.index(name) + 1]
