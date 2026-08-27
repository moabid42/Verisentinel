"""Bounded setup operations for the local execution capsule."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from execution.capsule.provider import CapsuleConfiguration, capsule_image_lock_path
from execution.capsule.runtime import DockerRuntime, DockerRuntimeError, ProcessOutput

_CAPSULE_DIRECTORY = Path(__file__).resolve().parent
_BUILD_TAG = "verisentinel-capsule:milestone03"


class CapsuleSetupRuntime(Protocol):
    """Docker operation needed to prepare the local capsule."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> ProcessOutput: ...


@dataclass(frozen=True, slots=True)
class CapsuleBuildReport:
    """Non-sensitive result of preparing the local capsule."""

    image: str
    network: str
    network_created: bool


class CapsuleBuilder:
    """Build the locked capsule image and prepare its internal network."""

    def __init__(
        self,
        configuration: CapsuleConfiguration | None = None,
        runtime: CapsuleSetupRuntime | None = None,
        image_lock_path: Path | None = None,
    ) -> None:
        self.configuration = configuration or CapsuleConfiguration.from_environment()
        self.runtime = runtime or DockerRuntime()
        self.image_lock_path = image_lock_path or capsule_image_lock_path()

    def build(self) -> CapsuleBuildReport:
        """Build and verify the image, then ensure the internal network exists."""
        self._require_success(
            (
                "build",
                "--network=none",
                "--pull=false",
                "--provenance=false",
                "--tag",
                _BUILD_TAG,
                str(_CAPSULE_DIRECTORY),
            ),
            timeout_seconds=300.0,
            failure="capsule image build failed",
        )
        image_id = self._require_success(
            ("image", "inspect", _BUILD_TAG, "--format", "{{.Id}}"),
            timeout_seconds=10.0,
            failure="built capsule image could not be inspected",
        ).stdout.decode(errors="replace").strip()
        image = f"verisentinel-capsule@{image_id}"
        CapsuleConfiguration(
            image=image,
            network=self.configuration.network,
            user_id=self.configuration.user_id,
            group_id=self.configuration.group_id,
        )
        network_created = self._ensure_network()
        self._activate_image(image)
        return CapsuleBuildReport(
            image=image,
            network=self.configuration.network,
            network_created=network_created,
        )

    def _activate_image(self, image: str) -> None:
        self.image_lock_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.image_lock_path.with_suffix(".lock.tmp")
        temporary.write_text(f"{image}\n", encoding="utf-8")
        temporary.replace(self.image_lock_path)

    def _ensure_network(self) -> bool:
        inspected = self._run(
            (
                "network",
                "inspect",
                self.configuration.network,
                "--format",
                "{{.Internal}}",
            ),
            timeout_seconds=10.0,
        )
        if inspected.return_code == 0:
            if inspected.stdout.strip() != b"true":
                raise DockerRuntimeError(
                    "configured capsule network exists but is not internal"
                )
            return False

        self._require_success(
            ("network", "create", "--internal", self.configuration.network),
            timeout_seconds=10.0,
            failure="capsule network creation failed",
        )
        return True

    def _require_success(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        failure: str,
    ) -> ProcessOutput:
        result = self._run(arguments, timeout_seconds=timeout_seconds)
        if result.return_code != 0:
            raise DockerRuntimeError(failure)
        return result

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessOutput:
        return self.runtime.run(
            arguments,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=131_072,
        )
