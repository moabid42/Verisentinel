"""Build and activate the fixed infrastructure gateway container."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.config import Paths
from core.models import ImmutableModel
from execution.capsule.provider import CapsuleConfiguration
from execution.capsule.runtime import DockerRuntime, DockerRuntimeError, ProcessOutput

_GATEWAY_DIRECTORY = Path(__file__).resolve().parent
_GATEWAY_TAG = "verisentinel-gateway:milestone03"
_GATEWAY_CONTAINER = "verisentinel-gateway"


class GatewayRuntime(Protocol):
    """Docker operations needed by gateway lifecycle management."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput: ...


@dataclass(frozen=True, slots=True)
class GatewayBuildReport:
    """Immutable gateway image selected by the latest build."""

    image: str


class GatewayConnectionConfiguration(ImmutableModel):
    """Non-sensitive fixed target mounted into the gateway."""

    infrastructure_path: str
    principal: str


def gateway_image_lock_path() -> Path:
    """Return the generated lock for the active gateway image."""
    return Paths().runtime / "gateway" / "image.lock"


class GatewayBuilder:
    """Build the gateway without network access and activate its digest."""

    def __init__(
        self,
        runtime: GatewayRuntime | None = None,
        image_lock_path: Path | None = None,
    ) -> None:
        self.runtime = runtime or DockerRuntime()
        self.image_lock_path = image_lock_path or gateway_image_lock_path()

    def build(self) -> GatewayBuildReport:
        """Build and lock the fixed gateway image."""
        self._require_success(
            (
                "build",
                "--network=none",
                "--pull=false",
                "--provenance=false",
                "--tag",
                _GATEWAY_TAG,
                str(_GATEWAY_DIRECTORY),
            ),
            timeout_seconds=300.0,
            failure="gateway image build failed",
        )
        image_id = (
            self._require_success(
                ("image", "inspect", _GATEWAY_TAG, "--format", "{{.Id}}"),
                timeout_seconds=10.0,
                failure="built gateway image could not be inspected",
            )
            .stdout.decode(errors="replace")
            .strip()
        )
        image = f"verisentinel-gateway@{image_id}"
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            raise DockerRuntimeError("gateway image digest is invalid")
        self.image_lock_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.image_lock_path.with_suffix(".lock.tmp")
        temporary.write_text(f"{image}\n", encoding="utf-8")
        temporary.replace(self.image_lock_path)
        return GatewayBuildReport(image=image)

    def _require_success(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        failure: str,
    ) -> ProcessOutput:
        result = self.runtime.run(
            arguments,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=131_072,
        )
        if result.return_code != 0:
            raise DockerRuntimeError(failure)
        return result


class GatewayManager:
    """Replace the active gateway with one exact scenario binding."""

    def __init__(
        self,
        *,
        runtime: GatewayRuntime | None = None,
        capsule: CapsuleConfiguration | None = None,
        image_lock_path: Path | None = None,
        configuration_path: Path | None = None,
    ) -> None:
        self.runtime = runtime or DockerRuntime()
        self.capsule = capsule or CapsuleConfiguration.from_environment()
        self.image_lock_path = image_lock_path or gateway_image_lock_path()
        self.configuration_path = configuration_path or (
            Paths().runtime / "gateway" / "connection.json"
        )

    def activate(self, *, infrastructure_path: str, principal: str) -> None:
        """Start the restricted gateway on bridge and capsule networks."""
        image = self._image()
        self._write_configuration(infrastructure_path, principal)
        self.runtime.run(
            ("container", "rm", "--force", "--volumes", _GATEWAY_CONTAINER),
            timeout_seconds=10.0,
            output_limit_bytes=16_384,
        )
        mounted = (
            f"type=bind,src={self.configuration_path.resolve()},"
            "dst=/run/verisentinel/connection.json,readonly"
        )
        started = self._run(
            (
                "run",
                "--detach",
                "--name",
                _GATEWAY_CONTAINER,
                "--pull",
                "never",
                "--network",
                "bridge",
                "--read-only",
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
                "--mount",
                mounted,
                image,
            )
        )
        if started.return_code != 0:
            raise DockerRuntimeError("infrastructure gateway could not start")
        connected = self._run(
            (
                "network",
                "connect",
                "--alias",
                "verisentinel-mock",
                self.capsule.network,
                _GATEWAY_CONTAINER,
            )
        )
        if connected.return_code != 0:
            self.runtime.run(
                ("container", "rm", "--force", "--volumes", _GATEWAY_CONTAINER),
                timeout_seconds=10.0,
                output_limit_bytes=16_384,
            )
            raise DockerRuntimeError("infrastructure gateway could not join the capsule network")

    def require_active(self, *, infrastructure_path: str, principal: str) -> None:
        """Require a running gateway with the exact persisted target binding."""
        try:
            configured = GatewayConnectionConfiguration.model_validate_json(
                self.configuration_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            raise DockerRuntimeError("infrastructure gateway is not configured") from None
        if (
            configured.infrastructure_path != infrastructure_path
            or configured.principal != principal
        ):
            raise DockerRuntimeError("infrastructure gateway does not match the active connection")
        result = self._run(
            (
                "container",
                "inspect",
                _GATEWAY_CONTAINER,
                "--format",
                "{{.State.Running}}|{{json .NetworkSettings.Networks}}",
            )
        )
        try:
            running, raw_networks = result.stdout.decode().strip().split("|", 1)
            networks = json.loads(raw_networks)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise DockerRuntimeError("infrastructure gateway is unavailable") from None
        if (
            result.return_code != 0
            or running != "true"
            or not isinstance(networks, dict)
            or "bridge" not in networks
            or self.capsule.network not in networks
        ):
            raise DockerRuntimeError("infrastructure gateway is unavailable")

    def deactivate(self) -> None:
        """Remove the fixed gateway and its generated connection configuration."""
        self.runtime.run(
            ("container", "rm", "--force", "--volumes", _GATEWAY_CONTAINER),
            timeout_seconds=10.0,
            output_limit_bytes=16_384,
        )
        self.configuration_path.unlink(missing_ok=True)

    def _image(self) -> str:
        try:
            image = self.image_lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise DockerRuntimeError(
                "gateway image is unavailable; run sandbox build first"
            ) from None
        if not image.startswith("verisentinel-gateway@sha256:"):
            raise DockerRuntimeError("gateway image lock is invalid")
        return image

    def _write_configuration(
        self,
        infrastructure_path: str,
        principal: str,
    ) -> None:
        configuration = GatewayConnectionConfiguration(
            infrastructure_path=infrastructure_path,
            principal=principal,
        )
        self.configuration_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.configuration_path.with_suffix(".json.tmp")
        temporary.write_text(configuration.model_dump_json(), encoding="utf-8")
        temporary.replace(self.configuration_path)
        self.configuration_path.chmod(0o444)

    def _run(self, arguments: tuple[str, ...]) -> ProcessOutput:
        return self.runtime.run(
            arguments,
            timeout_seconds=15.0,
            output_limit_bytes=131_072,
        )
