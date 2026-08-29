import json
import subprocess
from pathlib import Path

import pytest

import execution.gateway.entrypoint as gateway_entrypoint
from execution.capsule.provider import CapsuleConfiguration
from execution.capsule.runtime import DockerRuntimeError, ProcessOutput
from execution.gateway.entrypoint import GatewayError, Handler, load_configuration
from execution.gateway.manager import GatewayBuilder, GatewayManager

IMAGE_ID = "sha256:" + "a" * 64


class ScriptedRuntime:
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


def capsule(tmp_path: Path) -> CapsuleConfiguration:
    return CapsuleConfiguration(
        image="verisentinel-capsule@" + IMAGE_ID,
        network="verisentinel-capsule",
        user_id=1000,
        group_id=1000,
        temporary_root=tmp_path,
    )


def test_gateway_builder_activates_immutable_digest(tmp_path: Path) -> None:
    runtime = ScriptedRuntime([output(), output(stdout=IMAGE_ID.encode())])
    lock = tmp_path / "image.lock"

    report = GatewayBuilder(runtime, lock).build()

    assert report.image == "verisentinel-gateway@" + IMAGE_ID
    assert lock.read_text(encoding="utf-8").strip() == report.image
    assert runtime.calls[0][:4] == (
        "build",
        "--network=none",
        "--pull=false",
        "--provenance=false",
    )


def test_gateway_manager_joins_bridge_and_internal_network(tmp_path: Path) -> None:
    runtime = ScriptedRuntime([output(), output(), output()])
    lock = tmp_path / "image.lock"
    lock.write_text("verisentinel-gateway@" + IMAGE_ID, encoding="utf-8")
    configuration = tmp_path / "connection.json"
    manager = GatewayManager(
        runtime=runtime,
        capsule=capsule(tmp_path),
        image_lock_path=lock,
        configuration_path=configuration,
    )

    manager.activate(
        infrastructure_path="projects/project/buckets/scenario-target",
        principal="start@project.iam.gserviceaccount.com",
    )

    run_call = runtime.calls[1]
    assert run_call[0] == "run"
    assert run_call[run_call.index("--network") + 1] == "bridge"
    assert runtime.calls[2] == (
        "network",
        "connect",
        "--alias",
        "verisentinel-mock",
        "verisentinel-capsule",
        "verisentinel-gateway",
    )
    document = json.loads(configuration.read_text(encoding="utf-8"))
    assert document == {
        "infrastructure_path": "projects/project/buckets/scenario-target",
        "principal": "start@project.iam.gserviceaccount.com",
    }


def test_gateway_manager_requires_exact_running_binding(tmp_path: Path) -> None:
    networks = json.dumps({"bridge": {}, "verisentinel-capsule": {}}).encode()
    runtime = ScriptedRuntime([output(stdout=b"true|" + networks)])
    lock = tmp_path / "image.lock"
    lock.write_text("verisentinel-gateway@" + IMAGE_ID, encoding="utf-8")
    configuration = tmp_path / "connection.json"
    configuration.write_text(
        json.dumps(
            {
                "infrastructure_path": "projects/project/buckets/target",
                "principal": "start@project.iam.gserviceaccount.com",
            }
        ),
        encoding="utf-8",
    )
    manager = GatewayManager(
        runtime=runtime,
        capsule=capsule(tmp_path),
        image_lock_path=lock,
        configuration_path=configuration,
    )

    manager.require_active(
        infrastructure_path="projects/project/buckets/target",
        principal="start@project.iam.gserviceaccount.com",
    )

    assert runtime.calls[0][:3] == (
        "container",
        "inspect",
        "verisentinel-gateway",
    )


def test_gateway_manager_requires_built_image(tmp_path: Path) -> None:
    manager = GatewayManager(
        runtime=ScriptedRuntime([]),
        capsule=capsule(tmp_path),
        image_lock_path=tmp_path / "missing.lock",
        configuration_path=tmp_path / "connection.json",
    )

    with pytest.raises(DockerRuntimeError, match="sandbox build"):
        manager.activate(
            infrastructure_path="projects/project/buckets/scenario-target",
            principal="start@project.iam.gserviceaccount.com",
        )


def test_gateway_manager_deactivates_container_and_configuration(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime([output()])
    configuration = tmp_path / "connection.json"
    configuration.write_text("{}", encoding="utf-8")
    manager = GatewayManager(
        runtime=runtime,
        capsule=capsule(tmp_path),
        image_lock_path=tmp_path / "image.lock",
        configuration_path=configuration,
    )

    manager.deactivate()

    assert runtime.calls == [
        (
            "container",
            "rm",
            "--force",
            "--volumes",
            "verisentinel-gateway",
        )
    ]
    assert not configuration.exists()


def test_gateway_configuration_rejects_unexpected_fields(tmp_path: Path) -> None:
    path = tmp_path / "connection.json"
    path.write_text(
        json.dumps(
            {
                "infrastructure_path": "projects/project/buckets/target",
                "principal": "start@project.iam.gserviceaccount.com",
                "access_token": "must-not-be-accepted",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GatewayError, match="configuration invalid"):
        load_configuration(path)


def test_gateway_runs_exact_previewed_command_without_credential_arguments(
    monkeypatch,
) -> None:
    approval_id = "approval_" + "1" * 32
    command_stdout = b'{"bucket":"scenario-target","name":"action.json"}\n'
    calls: list[tuple[tuple[str, ...], dict]] = []

    def run(arguments: tuple[str, ...], **kwargs) -> subprocess.CompletedProcess:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(
            arguments,
            returncode=0,
            stdout=command_stdout,
            stderr=b"",
        )

    monkeypatch.setattr(gateway_entrypoint.subprocess, "run", run)
    handler = object.__new__(Handler)
    handler.configuration = {
        "infrastructure_path": "projects/project/buckets/scenario-target",
        "principal": "start@project.iam.gserviceaccount.com",
    }

    object_uri, stdout = handler._upload(
        {"approval_id": approval_id},
        "Bearer synthetic",
    )

    assert object_uri == (
        f"gs://scenario-target/actions/{approval_id}.json"
    )
    assert stdout == command_stdout.decode()
    arguments, options = calls[0]
    assert arguments == (
        "/usr/local/bin/python",
        "/opt/verisentinel/gcs_upload.py",
        "--bucket",
        "scenario-target",
        "--object",
        f"actions/{approval_id}.json",
    )
    assert "synthetic" not in " ".join(arguments)
    command_input = json.loads(options["input"])
    assert command_input["authorization"] == "Bearer synthetic"
    assert command_input["document"]["approval_id"] == approval_id
    assert options["capture_output"] is True
    assert options["check"] is False
