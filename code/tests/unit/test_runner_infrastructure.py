from datetime import UTC, datetime, timedelta
from pathlib import Path

from execution.credentials import (
    CredentialInspection,
    CredentialLease,
    CredentialSource,
    CredentialSourceKind,
)
from runner.infrastructure import (
    DevelopmentInfrastructure,
    InfrastructureRepository,
    InfrastructureService,
)
from runner.scenario import PlannerScenario

ACCESS_TOKEN = "development-access-token"
PRINCIPAL = "operator@example.test"
STARTING_PRINCIPAL = "start@example.iam.gserviceaccount.com"
NOW = datetime(2026, 8, 27, tzinfo=UTC)


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_bucket(self, **values: str) -> None:
        self.calls.append(values)


class StaticResolver:
    def __init__(self) -> None:
        self.registered: tuple[str, CredentialSource] | None = None

    def inspect(self, source: CredentialSource) -> CredentialInspection:
        return CredentialInspection(
            source_kind=source.kind,
            principal=PRINCIPAL,
            expires_at=NOW + timedelta(hours=1),
        )

    def register(self, reference: str, source: CredentialSource) -> None:
        self.registered = (reference, source)

    def resolve(self, reference: str, expected_principal: str) -> CredentialLease:
        assert self.registered is not None
        assert reference == self.registered[0]
        assert expected_principal == PRINCIPAL
        return CredentialLease(
            principal=PRINCIPAL,
            expires_at=NOW + timedelta(hours=1),
            source_kind=CredentialSourceKind.ADC,
            _access_token=ACCESS_TOKEN,
        )


def scenario() -> PlannerScenario:
    return PlannerScenario.model_validate(
        {
            "name": "development",
            "objective": "Evaluate storage paths",
            "operator": PRINCIPAL,
            "target_scope": "projects/security-sandbox",
            "infrastructure": {
                "path": "projects/security-sandbox/buckets/scenario-target"
            },
            "starting_service_account": {
                "identity": STARTING_PRINCIPAL,
                "credential_ref": "run/default",
                "permissions": ["storage.objects.create"],
            },
        }
    )


def test_create_provisions_scenario_bucket_and_persists_no_token(
    tmp_path: Path,
) -> None:
    repository = InfrastructureRepository(tmp_path)
    provider = RecordingProvider()
    service = InfrastructureService(
        repository,
        provider=provider,
        resolver=StaticResolver(),  # type: ignore[arg-type]
    )

    result = service.create(
        scenario(),
        CredentialSource(kind=CredentialSourceKind.ADC),
        location="europe-west3",
    )

    assert result.path == "projects/security-sandbox/buckets/scenario-target"
    assert result.starting_principal == STARTING_PRINCIPAL
    assert provider.calls == [
        {
            "project": "security-sandbox",
            "bucket": "scenario-target",
            "location": "europe-west3",
            "starting_principal": STARTING_PRINCIPAL,
            "access_token": ACCESS_TOKEN,
        }
    ]
    persisted = (tmp_path / f"{result.infrastructure_id}.json").read_text(
        encoding="utf-8"
    )
    assert ACCESS_TOKEN not in persisted
    assert repository.get(result.infrastructure_id) == result


def test_repository_lists_newest_infrastructure_first(tmp_path: Path) -> None:
    repository = InfrastructureRepository(tmp_path)
    first = DevelopmentInfrastructure(
        infrastructure_id="infra_" + "1" * 32,
        path="projects/security-sandbox/buckets/first-target",
        project="security-sandbox",
        bucket="first-target",
        location="EU",
        starting_principal=STARTING_PRINCIPAL,
        created_by=PRINCIPAL,
        created_at=NOW,
    )
    second = first.model_copy(
        update={
            "infrastructure_id": "infra_" + "2" * 32,
            "bucket": "second-target",
            "path": "projects/security-sandbox/buckets/second-target",
            "created_at": NOW + timedelta(seconds=1),
        }
    )
    repository.put(first)
    repository.put(second)

    assert repository.list() == (second, first)
