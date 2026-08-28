import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution.credentials import (
    CredentialInspection,
    CredentialLease,
    CredentialSource,
    CredentialSourceKind,
)
from runner.infrastructure import (
    DevelopmentInfrastructure,
    InfrastructureError,
    InfrastructureRepository,
    InfrastructureService,
    TerraformInfrastructureProvider,
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


class RecordingTerraformProvisioner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def provision(self, **values: object) -> None:
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


def test_create_uses_scenario_terraform_and_exact_principal(tmp_path: Path) -> None:
    repository = InfrastructureRepository(tmp_path / "records")
    api_provider = RecordingProvider()
    terraform = RecordingTerraformProvisioner()
    service = InfrastructureService(
        repository,
        provider=api_provider,
        terraform_provisioner=terraform,
        resolver=StaticResolver(),  # type: ignore[arg-type]
    )
    scenario_directory = tmp_path / "scenario"
    module_directory = scenario_directory / "terraform"
    module_directory.mkdir(parents=True)
    configured = scenario().model_copy(
        update={
            "infrastructure": scenario().infrastructure.model_copy(
                update={"terraform_root": "terraform"}
            )
        }
    )

    result = service.create(
        configured,
        CredentialSource(kind=CredentialSourceKind.ADC),
        location="EU",
        scenario_directory=scenario_directory,
    )

    assert api_provider.calls == []
    assert result.provisioner == "terraform"
    assert len(terraform.calls) == 1
    call = terraform.calls[0]
    state_directory = call.pop("state_directory")
    assert isinstance(state_directory, Path)
    assert state_directory.parent == tmp_path / "records" / "terraform"
    assert len(state_directory.name) == 64
    assert call == {
        "module_directory": module_directory,
        "project": "security-sandbox",
        "bucket": "scenario-target",
        "location": "EU",
        "starting_principal": STARTING_PRINCIPAL,
        "provisioner_member": "user:operator@example.test",
        "access_token": ACCESS_TOKEN,
    }


def test_terraform_provider_keeps_token_out_of_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module_directory = tmp_path / "module"
    module_directory.mkdir()
    (module_directory / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    (module_directory / ".terraform.lock.hcl").write_text("", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        calls.append((command, environment))
        output = ""
        if "output" in command:
            output = (
                '{"infrastructure_path":{"value":"projects/security-sandbox/'
                'buckets/scenario-target"},"starting_principal":{"value":"'
                f'{STARTING_PRINCIPAL}"}}}}'
            )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("runner.infrastructure.shutil.which", lambda _: "/bin/terraform")
    monkeypatch.setattr("runner.infrastructure.subprocess.run", run)
    provider = TerraformInfrastructureProvider()

    provider.provision(
        module_directory=module_directory,
        state_directory=tmp_path / "state",
        project="security-sandbox",
        bucket="scenario-target",
        location="EU",
        starting_principal=STARTING_PRINCIPAL,
        provisioner_member="user:operator@example.test",
        access_token=ACCESS_TOKEN,
    )

    assert [call[0][2] for call in calls] == ["init", "plan", "apply", "output"]
    assert all(ACCESS_TOKEN not in argument for command, _ in calls for argument in command)
    assert all(environment["GOOGLE_OAUTH_ACCESS_TOKEN"] == ACCESS_TOKEN for _, environment in calls)


def test_terraform_provider_finds_virtual_environment_sibling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module_directory = tmp_path / "module"
    module_directory.mkdir()
    (module_directory / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    (module_directory / ".terraform.lock.hcl").write_text("", encoding="utf-8")
    sibling_binary = tmp_path / "bin" / "terraform"
    sibling_binary.parent.mkdir()
    sibling_binary.touch()
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(command)
        output = ""
        if "output" in command:
            output = (
                '{"infrastructure_path":{"value":"projects/security-sandbox/'
                'buckets/scenario-target"},"starting_principal":{"value":"'
                f'{STARTING_PRINCIPAL}"}}}}'
            )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("runner.infrastructure.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "runner.infrastructure.sys.executable",
        str(sibling_binary.with_name("python")),
    )
    monkeypatch.setattr("runner.infrastructure.subprocess.run", run)

    TerraformInfrastructureProvider().provision(
        module_directory=module_directory,
        state_directory=tmp_path / "state",
        project="security-sandbox",
        bucket="scenario-target",
        location="EU",
        starting_principal=STARTING_PRINCIPAL,
        provisioner_member="user:operator@example.test",
        access_token=ACCESS_TOKEN,
    )

    assert all(command[0] == str(sibling_binary) for command in commands)


def test_terraform_provider_sanitizes_command_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module_directory = tmp_path / "module"
    module_directory.mkdir()
    (module_directory / "main.tf").write_text("terraform {}\n", encoding="utf-8")
    (module_directory / ".terraform.lock.hcl").write_text("", encoding="utf-8")

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.CalledProcessError(1, ["terraform"], stderr=ACCESS_TOKEN)

    monkeypatch.setattr("runner.infrastructure.shutil.which", lambda _: "/bin/terraform")
    monkeypatch.setattr("runner.infrastructure.subprocess.run", fail)
    provider = TerraformInfrastructureProvider()

    with pytest.raises(InfrastructureError, match="could not initialize") as raised:
        provider.provision(
            module_directory=module_directory,
            state_directory=tmp_path / "state",
            project="security-sandbox",
            bucket="scenario-target",
            location="EU",
            starting_principal=STARTING_PRINCIPAL,
            provisioner_member="user:operator@example.test",
            access_token=ACCESS_TOKEN,
        )

    assert ACCESS_TOKEN not in str(raised.value)
