"""Docker integration coverage for one approved local-capsule execution."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.config import Paths
from core.errors import AuthorizationError
from core.models import ApprovalRecord, ExecutionRequest
from core.security import stable_digest
from execution.capsule.provider import (
    CapsuleConfiguration,
    CapsuleExecutionProvider,
)
from execution.capsule.runtime import DockerRuntime
from execution.credentials import (
    CredentialResolver,
    TokenMetadata,
    parse_credential_source,
)
from execution.models import EngagementAuthorization, ExecutionAttemptStatus
from execution.repository import ExecutionRepository
from execution.service import ExecutionService
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository

HERE = Path(__file__).resolve().parents[1]
CODE_ROOT = HERE.parent
CAPSULE_DIRECTORY = CODE_ROOT / "execution" / "capsule"
MOCK_ENDPOINT = HERE / "fixtures" / "capsule_mock_endpoint.py"
BASE_IMAGE = (
    "python@sha256:"
    "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
)
ACCESS_TOKEN = "synthetic-capsule-integration-token"
PRINCIPAL = "operator@example.test"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class StaticTokenInspector:
    """Return deterministic token metadata without a network request."""

    def inspect(self, access_token: str) -> TokenMetadata:
        assert access_token == ACCESS_TOKEN
        return TokenMetadata(
            principal=PRINCIPAL,
            expires_at=NOW + timedelta(minutes=10),
        )


class RecordingDockerRuntime(DockerRuntime):
    """Record the exact capsule name while using the real Docker runtime."""

    def __init__(self) -> None:
        super().__init__()
        self.capsule_name = ""

    def run_container(
        self,
        arguments,
        *,
        container_name,
        timeout_seconds,
        output_limit_bytes,
    ):
        self.capsule_name = container_name
        return super().run_container(
            arguments,
            container_name=container_name,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    for image in (BASE_IMAGE,):
        result = subprocess.run(
            ("docker", "image", "inspect", image),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            return False
    return (
        subprocess.run(
            ("docker", "info"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon and pinned base image are required",
)


def test_approved_capsule_execution_is_isolated_and_consumed(
    tmp_path: Path,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    network = f"verisentinel-test-{suffix}"
    endpoint = f"verisentinel-mock-{suffix}"
    input_root = tmp_path / "capsule-inputs"
    input_root.mkdir()
    token_path = tmp_path / "endpoint-token"
    token_path.write_text(ACCESS_TOKEN, encoding="utf-8")
    token_path.chmod(0o400)
    image = _build_locked_image()
    _run(("docker", "network", "create", "--internal", network))
    try:
        _start_endpoint(endpoint, network, token_path)
        _wait_for_endpoint(endpoint)
        runtime = RecordingDockerRuntime()
        capsule_provider = CapsuleExecutionProvider(
            configuration=CapsuleConfiguration(
                image=image,
                network=network,
                user_id=os.getuid(),
                group_id=os.getgid(),
                temporary_root=input_root,
            ),
            runtime=runtime,
        )
        service, request = _execution_service(
            tmp_path,
            capsule_provider,
        )

        result = service.execute(request)

        assert result.provider == "capsule"
        assert result.observation.success
        assert result.observation.execution_id == "capsule-approval"
        attempt = service.repository.attempt_by_approval(request.approval_id)
        assert attempt.status == ExecutionAttemptStatus.SUCCEEDED
        assert attempt.execution_id == result.observation.execution_id
        assert runtime.capsule_name
        assert not _container_exists(runtime.capsule_name)
        assert tuple(input_root.iterdir()) == ()
        assert capsule_provider.last_metrics is not None
        assert capsule_provider.last_metrics.startup_latency_seconds is not None
        assert capsule_provider.last_metrics.peak_memory_bytes is not None
        assert capsule_provider.last_metrics.peak_memory_bytes > 0
        persisted = "".join(
            path.read_text(encoding="utf-8")
            for path in (tmp_path / "execution").rglob("*.json")
        )
        assert ACCESS_TOKEN not in persisted
    finally:
        _run(
            ("docker", "container", "rm", "--force", endpoint),
            check=False,
        )
        _run(("docker", "network", "rm", network), check=False)


def test_endpoint_failure_consumes_approval_and_cleans_capsule(
    tmp_path: Path,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    network = f"verisentinel-failure-{suffix}"
    input_root = tmp_path / "capsule-inputs"
    input_root.mkdir()
    image = _build_locked_image()
    _run(("docker", "network", "create", "--internal", network))
    runtime = RecordingDockerRuntime()
    capsule_provider = CapsuleExecutionProvider(
        configuration=CapsuleConfiguration(
            image=image,
            network=network,
            user_id=os.getuid(),
            group_id=os.getgid(),
            temporary_root=input_root,
        ),
        runtime=runtime,
    )
    service, request = _execution_service(tmp_path, capsule_provider)
    try:
        with pytest.raises(AuthorizationError, match="execution provider failed"):
            service.execute(request)

        attempt = service.repository.attempt_by_approval(request.approval_id)
        assert attempt.status == ExecutionAttemptStatus.FAILED
        assert attempt.failure_code == "provider_execution_failed"
        with pytest.raises(AuthorizationError, match="already been consumed"):
            service.execute(request)
        assert runtime.capsule_name
        assert not _container_exists(runtime.capsule_name)
        assert tuple(input_root.iterdir()) == ()
        assert capsule_provider.last_metrics is not None
        assert capsule_provider.last_metrics.total_duration_seconds > 0
        assert capsule_provider.last_metrics.cleanup_duration_seconds > 0
    finally:
        if runtime.capsule_name:
            _run(
                ("docker", "container", "rm", "--force", runtime.capsule_name),
                check=False,
            )
        _run(("docker", "network", "rm", network), check=False)


def _build_locked_image() -> str:
    image = (CAPSULE_DIRECTORY / "image.lock").read_text(encoding="utf-8").strip()
    _run(
        (
            "docker",
            "build",
            "--network=none",
            "--pull=false",
            "--provenance=false",
            "--tag",
            "verisentinel-capsule:integration",
            str(CAPSULE_DIRECTORY),
        )
    )
    identifier = _run(
        ("docker", "image", "inspect", image, "--format", "{{.Id}}")
    ).stdout.strip()
    assert identifier == image.rsplit("@", maxsplit=1)[1]
    return image


def _start_endpoint(name: str, network: str, token_path: Path) -> None:
    user = f"{token_path.stat().st_uid}:{token_path.stat().st_gid}"
    _run(
        (
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            network,
            "--network-alias",
            "verisentinel-mock",
            "--read-only",
            "--user",
            user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "32",
            "--memory",
            "67108864",
            "--memory-swap",
            "67108864",
            "--cpus",
            "0.25",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=8388608,mode=1777",
            "--mount",
            (
                f"type=bind,src={MOCK_ENDPOINT},"
                "dst=/tmp/mock_endpoint.py,readonly"
            ),
            "--mount",
            (
                f"type=bind,src={token_path},"
                "dst=/tmp/expected-token,readonly"
            ),
            BASE_IMAGE,
            "/usr/local/bin/python",
            "/tmp/mock_endpoint.py",
        )
    )


def _wait_for_endpoint(name: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        result = _run(
            (
                "docker",
                "exec",
                name,
                "/usr/local/bin/python",
                "-c",
                (
                    "import socket; "
                    "socket.create_connection(('127.0.0.1', 8080), 0.2).close()"
                ),
            ),
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.05)
    raise AssertionError("mock endpoint did not become ready")


def _execution_service(
    tmp_path: Path,
    provider: CapsuleExecutionProvider,
) -> tuple[ExecutionService, ExecutionRequest]:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    technique = next(iter(matrix.techniques.values()))
    request = ExecutionRequest(
        engagement_id="engagement",
        candidate_id="candidate",
        action_id=f"technique:{technique.technique_id}",
        identity=PRINCIPAL,
        target="projects/sandbox",
        validator_result_id="validation",
        approval_id="approval",
        state_version="state",
        matrix_version=matrix.matrix_version,
        credential_ref="run/default",
    )
    approval = ApprovalRecord(
        approval_id=request.approval_id,
        engagement_id=request.engagement_id,
        candidate_id=request.candidate_id,
        action_id=request.action_id,
        identity=request.identity,
        target=request.target,
        arguments_digest=stable_digest(request.arguments.model_dump(mode="json")),
        validator_result_id=request.validator_result_id,
        state_version=request.state_version,
        matrix_version=request.matrix_version,
        operator="operator@example.test",
        credential_ref=request.credential_ref,
    )
    resolver = CredentialResolver(
        environ={"CAPSULE_TOKEN": ACCESS_TOKEN},
        token_inspector=StaticTokenInspector(),
        now=lambda: NOW,
    )
    resolver.register(
        request.credential_ref,
        parse_credential_source("env:CAPSULE_TOKEN"),
    )
    service = ExecutionService(
        repository=ExecutionRepository(tmp_path / "execution"),
        snapshots=snapshots,
        provider_impl=provider,
        credential_resolver=resolver,
        enabled=True,
    )
    service.authorize(
        EngagementAuthorization(
            engagement_id=request.engagement_id,
            allowed_targets=(request.target,),
            state_version=request.state_version,
            matrix_version=request.matrix_version,
        )
    )
    service.register_approval(approval)
    return service, request


def _container_exists(name: str) -> bool:
    return (
        _run(
            ("docker", "container", "inspect", name),
            check=False,
        ).returncode
        == 0
    )


def _run(
    arguments: tuple[str, ...],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=check,
    )
