from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.errors import AuthorizationError
from execution.credentials import (
    CredentialLease,
    CredentialSource,
    CredentialSourceKind,
)
from runner.connection import (
    ConnectionMode,
    SandboxConnectionRepository,
    SandboxConnectionService,
)
from runner.infrastructure import (
    DevelopmentInfrastructure,
    InfrastructureRepository,
)
from runner.scenario import PlannerScenario

ACCESS_TOKEN = "scenario-service-account-token"
NOW = datetime(2026, 8, 27, tzinfo=UTC)
PRINCIPAL = "start@security-sandbox.iam.gserviceaccount.com"


class StaticResolver:
    def register(self, reference: str, source: CredentialSource) -> None:
        assert reference == "run/default"
        assert source.kind == CredentialSourceKind.STDIN

    def resolve(self, reference: str, expected_principal: str) -> CredentialLease:
        assert reference == "run/default"
        assert expected_principal == PRINCIPAL
        return CredentialLease(
            principal=PRINCIPAL,
            expires_at=NOW + timedelta(hours=1),
            source_kind=CredentialSourceKind.STDIN,
            _access_token=ACCESS_TOKEN,
        )


class RecordingProbe:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def verify_create_access(self, **values: str) -> None:
        self.calls.append(values)


class RecordingActivator:
    def __init__(self, *, fails: bool = False) -> None:
        self.calls: list[dict[str, str]] = []
        self.fails = fails

    def activate(self, **values: str) -> None:
        self.calls.append(values)
        if self.fails:
            raise RuntimeError("gateway unavailable")

    def deactivate(self) -> None:
        self.calls.append({"operation": "deactivate"})


def scenario() -> PlannerScenario:
    return PlannerScenario.model_validate(
        {
            "name": "connected-scenario",
            "objective": "Evaluate storage paths",
            "operator": "human@example.test",
            "target_scope": "projects/security-sandbox",
            "infrastructure": {
                "path": "projects/security-sandbox/buckets/scenario-target"
            },
            "starting_service_account": {
                "identity": PRINCIPAL,
                "credential_ref": "run/default",
                "permissions": ["storage.objects.create"],
            },
        }
    )


def test_remote_connection_verifies_principal_and_persists_no_token(
    tmp_path: Path,
) -> None:
    repository = SandboxConnectionRepository(tmp_path / "connections")
    probe = RecordingProbe()
    activator = RecordingActivator()
    service = SandboxConnectionService(
        repository,
        resolver=StaticResolver(),  # type: ignore[arg-type]
        probe=probe,
        activator=activator,
    )

    result = service.connect(
        scenario(),
        CredentialSource(kind=CredentialSourceKind.STDIN),
        development_mode=False,
        scenario_path=tmp_path / "scenario.yaml",
    )

    assert result.mode == ConnectionMode.REMOTE
    assert result.infrastructure_id is None
    assert result.principal == PRINCIPAL
    assert result.scenario_path == str((tmp_path / "scenario.yaml").resolve())
    assert probe.calls[0]["access_token"] == ACCESS_TOKEN
    assert activator.calls == [
        {
            "infrastructure_path": (
                "projects/security-sandbox/buckets/scenario-target"
            ),
            "principal": PRINCIPAL,
        }
    ]
    persisted = (tmp_path / "connections" / "active.json").read_text(
        encoding="utf-8"
    )
    assert ACCESS_TOKEN not in persisted
    assert repository.active() == result


def test_development_connection_requires_matching_infrastructure(
    tmp_path: Path,
) -> None:
    infrastructure = InfrastructureRepository(tmp_path / "infrastructure")
    record = DevelopmentInfrastructure(
        infrastructure_id="infra_" + "1" * 32,
        path="projects/security-sandbox/buckets/another-target",
        project="security-sandbox",
        bucket="another-target",
        location="EU",
        starting_principal=PRINCIPAL,
        created_by="operator@example.test",
    )
    infrastructure.put(record)
    service = SandboxConnectionService(
        SandboxConnectionRepository(tmp_path / "connections"),
        infrastructure=infrastructure,
        resolver=StaticResolver(),  # type: ignore[arg-type]
        probe=RecordingProbe(),
    )

    with pytest.raises(AuthorizationError, match="scenario path"):
        service.connect(
            scenario(),
            CredentialSource(kind=CredentialSourceKind.STDIN),
            development_mode=True,
            infrastructure_id=record.infrastructure_id,
        )


def test_development_connection_uses_matching_opaque_id(tmp_path: Path) -> None:
    infrastructure = InfrastructureRepository(tmp_path / "infrastructure")
    record = DevelopmentInfrastructure(
        infrastructure_id="infra_" + "2" * 32,
        path=scenario().infrastructure.path,
        project="security-sandbox",
        bucket="scenario-target",
        location="EU",
        starting_principal=PRINCIPAL,
        created_by="operator@example.test",
    )
    infrastructure.put(record)
    service = SandboxConnectionService(
        SandboxConnectionRepository(tmp_path / "connections"),
        infrastructure=infrastructure,
        resolver=StaticResolver(),  # type: ignore[arg-type]
        probe=RecordingProbe(),
    )

    result = service.connect(
        scenario(),
        CredentialSource(kind=CredentialSourceKind.STDIN),
        development_mode=True,
        infrastructure_id=record.infrastructure_id,
    )

    assert result.mode == ConnectionMode.DEVELOPMENT
    assert result.infrastructure_id == record.infrastructure_id


def test_remote_connection_rejects_development_id(tmp_path: Path) -> None:
    service = SandboxConnectionService(
        SandboxConnectionRepository(tmp_path),
        resolver=StaticResolver(),  # type: ignore[arg-type]
        probe=RecordingProbe(),
    )

    with pytest.raises(ValueError, match="only with the --dev option"):
        service.connect(
            scenario(),
            CredentialSource(kind=CredentialSourceKind.STDIN),
            development_mode=False,
            infrastructure_id="infra_" + "3" * 32,
        )


def test_connection_is_not_persisted_when_gateway_activation_fails(
    tmp_path: Path,
) -> None:
    repository = SandboxConnectionRepository(tmp_path)
    service = SandboxConnectionService(
        repository,
        resolver=StaticResolver(),  # type: ignore[arg-type]
        probe=RecordingProbe(),
        activator=RecordingActivator(fails=True),
    )

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        service.connect(
            scenario(),
            CredentialSource(kind=CredentialSourceKind.STDIN),
            development_mode=False,
        )

    assert repository.optional_active() is None


def test_disconnect_deactivates_gateway_and_clears_record(tmp_path: Path) -> None:
    repository = SandboxConnectionRepository(tmp_path)
    activator = RecordingActivator()
    service = SandboxConnectionService(
        repository,
        resolver=StaticResolver(),  # type: ignore[arg-type]
        probe=RecordingProbe(),
        activator=activator,
    )
    connection = service.connect(
        scenario(),
        CredentialSource(kind=CredentialSourceKind.STDIN),
        development_mode=False,
    )

    disconnected = service.disconnect()

    assert disconnected == connection
    assert activator.calls[-1] == {"operation": "deactivate"}
    assert repository.optional_active() is None
