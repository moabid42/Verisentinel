"""Tests for CLI-facing application services."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import runner.application as application_module
from core.config import Paths
from core.models import MatrixSnapshot
from execution.capsule.doctor import CapsuleCheck, CapsuleDoctorReport
from execution.capsule.setup import CapsuleBuildReport
from execution.credentials import (
    CredentialResolver,
    CredentialSourceKind,
    TokenMetadata,
    parse_credential_source,
)
from runner.application import (
    analyse_connected_environment,
    build_corpus,
    build_sandbox,
    inspect_authentication,
    inspect_sandbox,
    read_connected_environment,
    read_corpus_status,
    validate_scenario,
)
from runner.connection import (
    ConnectionMode,
    SandboxConnection,
    SandboxConnectionRepository,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
PRINCIPAL = "runner@authorized-project.iam.gserviceaccount.com"
ACCESS_TOKEN = "synthetic-application-token"


class StaticTokenInspector:
    """Return deterministic public metadata for a supplied token."""

    def inspect(self, access_token: str) -> TokenMetadata:
        assert access_token == ACCESS_TOKEN
        return TokenMetadata(
            principal=PRINCIPAL,
            expires_at=NOW + timedelta(minutes=10),
        )


class StubIngestor:
    """Expose a fixed snapshot through the corpus service boundary."""

    def __init__(self) -> None:
        self.snapshot = SimpleNamespace(
            matrix_version="sha256:matrix",
            permissions=("permission.one", "permission.two"),
            detections={"detection": object()},
            techniques={"technique": object()},
        )

    def build(self, request: object) -> MatrixSnapshot:
        del request
        return cast(MatrixSnapshot, self.snapshot)

    def current(self) -> MatrixSnapshot:
        return cast(MatrixSnapshot, self.snapshot)


class StubDoctor:
    """Return a fixed capsule readiness result without invoking Docker."""

    def inspect(self) -> CapsuleDoctorReport:
        return CapsuleDoctorReport(
            available=False,
            provider="capsule",
            detail="Local execution capsule is not ready.",
            checks=(
                CapsuleCheck(
                    name="runtime",
                    passed=False,
                    detail="Docker daemon is unavailable",
                ),
            ),
        )


class StubBuilder:
    """Return a fixed setup result without invoking Docker."""

    def build(self) -> CapsuleBuildReport:
        return CapsuleBuildReport(
            image="verisentinel-capsule@sha256:" + "a" * 64,
            network="verisentinel-capsule",
            network_created=True,
        )


def test_scenario_validation_returns_validated_model(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        """name: command-test
objective: Evaluate one path
operator: operator@example.test
target_scope: projects/authorized-project
infrastructure:
  path: projects/authorized-project/buckets/scenario-target
starting_service_account:
  identity: runner@authorized-project.iam.gserviceaccount.com
  credential_ref: run/default
  permissions:
    - storage.objects.get
""",
        encoding="utf-8",
    )

    scenario = validate_scenario(path)

    assert scenario.name == "command-test"


def test_authentication_inspection_returns_no_credential_material() -> None:
    resolver = CredentialResolver(
        environ={"VERISENTINEL_TOKEN": ACCESS_TOKEN},
        token_inspector=StaticTokenInspector(),
        now=lambda: NOW,
    )

    result = inspect_authentication(
        parse_credential_source("env:VERISENTINEL_TOKEN"),
        resolver=resolver,
    )

    assert result.source_kind == CredentialSourceKind.ENV
    assert result.principal == PRINCIPAL
    assert ACCESS_TOKEN not in repr(result)


def test_corpus_commands_return_bounded_snapshot_metadata() -> None:
    service = StubIngestor()

    built = build_corpus(service=service)
    current = read_corpus_status(service=service)

    assert built == current
    assert built.available
    assert built.matrix_version == "sha256:matrix"
    assert built.permission_count == 2
    assert built.detection_count == 1
    assert built.technique_count == 1


def test_sandbox_status_is_explicitly_unavailable() -> None:
    status = inspect_sandbox(doctor=StubDoctor())

    assert not status.available
    assert status.provider == "capsule"
    assert "not ready" in status.detail
    assert status.checks[0].name == "runtime"


def test_sandbox_build_returns_prepared_resources() -> None:
    report = build_sandbox(builder=StubBuilder())

    assert report.network == "verisentinel-capsule"
    assert report.network_created


def _connect_test_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, SandboxConnection]:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("IAM_PLANNER_RUNTIME", str(runtime))
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        """name: connected-scenario
objective: Evaluate one storage path
operator: operator@example.test
target_scope: projects/authorized-project
infrastructure:
  path: projects/authorized-project/buckets/scenario-target
starting_service_account:
  identity: runner@authorized-project.iam.gserviceaccount.com
  credential_ref: run/default
  permissions:
    - storage.objects.create
detections:
  sources:
    - sigma
""",
        encoding="utf-8",
    )
    connection = SandboxConnection(
        connection_id="connection_" + "1" * 32,
        mode=ConnectionMode.DEVELOPMENT,
        infrastructure_id="infra_" + "2" * 32,
        infrastructure_path=(
            "projects/authorized-project/buckets/scenario-target"
        ),
        scenario_name="connected-scenario",
        scenario_path=str(scenario_path),
        principal=PRINCIPAL,
        credential_ref="run/default",
        source_kind=CredentialSourceKind.IMPERSONATE,
    )
    SandboxConnectionRepository(runtime / "sandbox" / "connection").put(
        connection
    )
    return scenario_path, connection


def test_connected_environment_shows_declared_identity_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, connection = _connect_test_scenario(monkeypatch, tmp_path)

    environment = read_connected_environment(paths=Paths(repository=tmp_path))

    assert environment.connection_id == connection.connection_id
    assert environment.identity == PRINCIPAL
    assert environment.permissions == ("storage.objects.create",)
    assert environment.detection_sources == ("sigma",)
    assert environment.engagement_id is None


def test_connected_analysis_reconstructs_exact_impersonation_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario_path, connection = _connect_test_scenario(monkeypatch, tmp_path)
    requests = []
    monkeypatch.setattr(
        application_module,
        "run_planner",
        lambda request: requests.append(request) or 0,
    )

    result = analyse_connected_environment(paths=Paths(repository=tmp_path))

    assert result == 0
    assert requests[0].scenario_path == scenario_path
    assert requests[0].credential_source.kind == CredentialSourceKind.IMPERSONATE
    assert requests[0].credential_source.locator == connection.principal
    assert requests[0].development_mode
