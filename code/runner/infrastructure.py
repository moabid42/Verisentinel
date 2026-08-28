"""Disposable GCP infrastructure for development-mode scenario runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import Field

from core.ids import new_id
from core.models import ImmutableModel, utc_now
from core.persistence import JsonModelStore
from execution.credentials import CredentialResolver, CredentialSource
from runner.scenario import PlannerScenario

_INFRASTRUCTURE_ID_PATTERN = re.compile(r"^infra_[0-9a-f]{32}$")
_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_STORAGE_API = "https://storage.googleapis.com/storage/v1"


class InfrastructureError(RuntimeError):
    """A development infrastructure operation failed at its provider boundary."""


class DevelopmentInfrastructure(ImmutableModel):
    """Non-sensitive record of one disposable GCP scenario target."""

    infrastructure_id: str = Field(pattern=_INFRASTRUCTURE_ID_PATTERN.pattern)
    path: str = Field(min_length=1, max_length=512)
    project: str = Field(min_length=1, max_length=64)
    bucket: str = Field(min_length=3, max_length=63)
    location: str = Field(min_length=1, max_length=32)
    starting_principal: str = Field(min_length=1, max_length=320)
    created_by: str = Field(min_length=1, max_length=320)
    provisioner: Literal["gcp-api", "terraform"] = "gcp-api"
    created_at: datetime = Field(default_factory=utc_now)


class InfrastructureProvider(Protocol):
    """Provider operations needed to prepare a disposable scenario target."""

    def create_bucket(
        self,
        *,
        project: str,
        bucket: str,
        location: str,
        starting_principal: str,
        access_token: str,
    ) -> None: ...


class TerraformProvisioner(Protocol):
    """Terraform operations needed to prepare a scenario-owned target."""

    def provision(
        self,
        *,
        module_directory: Path,
        state_directory: Path,
        project: str,
        bucket: str,
        location: str,
        starting_principal: str,
        provisioner_member: str,
        access_token: str,
    ) -> None: ...


class GcpInfrastructureProvider:
    """Create a private, short-lived Cloud Storage action sink."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def create_bucket(
        self,
        *,
        project: str,
        bucket: str,
        location: str,
        starting_principal: str,
        access_token: str,
    ) -> None:
        headers = {"Authorization": f"Bearer {access_token}"}
        payload = {
            "name": bucket,
            "location": location,
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            },
            "lifecycle": {
                "rule": [
                    {
                        "action": {"type": "Delete"},
                        "condition": {"age": 1},
                    }
                ]
            },
        }
        self._request(
            "POST",
            f"{_STORAGE_API}/b",
            headers=headers,
            params={"project": project},
            json=payload,
            operation="create the development bucket",
        )
        policy = self._request(
            "GET",
            f"{_STORAGE_API}/b/{bucket}/iam",
            headers=headers,
            operation="read the development bucket policy",
        ).json()
        if not isinstance(policy, dict) or not isinstance(
            policy.get("bindings", []),
            list,
        ):
            raise InfrastructureError("GCP returned an invalid bucket policy")
        bindings = list(policy.get("bindings", []))
        bindings.append(
            {
                "role": "roles/storage.objectCreator",
                "members": [f"serviceAccount:{starting_principal}"],
            }
        )
        policy["bindings"] = bindings
        self._request(
            "PUT",
            f"{_STORAGE_API}/b/{bucket}/iam",
            headers=headers,
            json=policy,
            operation="bind the scenario service account",
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        **kwargs: object,
    ) -> httpx.Response:
        try:
            if self._client is None:
                response = httpx.request(method, url, timeout=20.0, **kwargs)
            else:
                response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, ValueError) as error:
            raise InfrastructureError(f"GCP could not {operation}") from error


class TerraformInfrastructureProvider:
    """Apply a reviewed scenario-local Terraform root with transient auth."""

    def __init__(self, binary: str = "terraform", timeout_seconds: float = 300.0) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    def provision(
        self,
        *,
        module_directory: Path,
        state_directory: Path,
        project: str,
        bucket: str,
        location: str,
        starting_principal: str,
        provisioner_member: str,
        access_token: str,
    ) -> None:
        """Initialize, plan, apply, and verify one Terraform root."""
        binary = shutil.which(self.binary)
        if binary is None:
            raise InfrastructureError(
                "Terraform CLI is required for this scenario infrastructure"
            )
        if not module_directory.is_dir() or not any(module_directory.glob("*.tf")):
            raise InfrastructureError("scenario Terraform root contains no .tf files")
        if not (module_directory / ".terraform.lock.hcl").is_file():
            raise InfrastructureError("scenario Terraform root has no provider lock file")

        state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_path = state_directory / "terraform.tfstate"
        plan_path = state_directory / "terraform.tfplan"
        environment = {
            "CHECKPOINT_DISABLE": "1",
            "GOOGLE_OAUTH_ACCESS_TOKEN": access_token,
            "PATH": os.environ.get("PATH", ""),
            "TF_DATA_DIR": str(state_directory / ".terraform"),
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
        }
        prefix = [binary, f"-chdir={module_directory}"]
        variables = [
            f"-var=project_id={project}",
            f"-var=bucket_name={bucket}",
            f"-var=location={location}",
            f"-var=starting_principal={starting_principal}",
            f"-var=provisioner_member={provisioner_member}",
        ]
        self._run(
            [*prefix, "init", "-input=false", "-lockfile=readonly", "-no-color"],
            environment,
            "initialize the scenario Terraform root",
        )
        self._run(
            [
                *prefix,
                "plan",
                "-input=false",
                "-no-color",
                f"-out={plan_path}",
                f"-state={state_path}",
                *variables,
            ],
            environment,
            "plan the scenario infrastructure",
        )
        self._run(
            [*prefix, "apply", "-input=false", "-no-color", str(plan_path)],
            environment,
            "apply the scenario infrastructure",
        )
        output = self._run(
            [
                *prefix,
                "output",
                "-json",
                "-no-color",
                f"-state={state_path}",
            ],
            environment,
            "read the scenario infrastructure outputs",
        )
        self._verify_outputs(
            output,
            infrastructure_path=f"projects/{project}/buckets/{bucket}",
            starting_principal=starting_principal,
        )
        plan_path.unlink(missing_ok=True)

    def _run(
        self,
        command: list[str],
        environment: dict[str, str],
        operation: str,
    ) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=True,
                env=environment,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise InfrastructureError(f"Terraform could not {operation}") from error
        return result.stdout

    @staticmethod
    def _verify_outputs(
        document: str,
        *,
        infrastructure_path: str,
        starting_principal: str,
    ) -> None:
        try:
            outputs = json.loads(document)
            actual_path = outputs["infrastructure_path"]["value"]
            actual_principal = outputs["starting_principal"]["value"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise InfrastructureError("Terraform returned invalid scenario outputs") from error
        if actual_path != infrastructure_path or actual_principal != starting_principal:
            raise InfrastructureError("Terraform outputs do not match the scenario")


class InfrastructureRepository:
    """Persist non-sensitive development infrastructure records."""

    def __init__(self, directory: Path) -> None:
        self.store = JsonModelStore(directory, DevelopmentInfrastructure)

    def put(self, infrastructure: DevelopmentInfrastructure) -> None:
        self.store.put(infrastructure.infrastructure_id, infrastructure)

    def get(self, infrastructure_id: str) -> DevelopmentInfrastructure:
        if _INFRASTRUCTURE_ID_PATTERN.fullmatch(infrastructure_id) is None:
            raise ValueError("infrastructure ID has an invalid format")
        return self.store.get(infrastructure_id)

    def list(self) -> tuple[DevelopmentInfrastructure, ...]:
        records = tuple(self.store.get(key) for key in self.store.list_keys())
        return tuple(
            sorted(
                records,
                key=lambda record: (record.created_at, record.infrastructure_id),
                reverse=True,
            )
        )


class InfrastructureService:
    """Provision scenario-bound development infrastructure through ADC."""

    def __init__(
        self,
        repository: InfrastructureRepository,
        *,
        provider: InfrastructureProvider | None = None,
        terraform_provisioner: TerraformProvisioner | None = None,
        resolver: CredentialResolver | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or GcpInfrastructureProvider()
        self.terraform_provisioner = (
            terraform_provisioner or TerraformInfrastructureProvider()
        )
        self.resolver = resolver or CredentialResolver()

    def create(
        self,
        scenario: PlannerScenario,
        source: CredentialSource,
        *,
        location: str,
        scenario_directory: Path | None = None,
    ) -> DevelopmentInfrastructure:
        """Create and persist one disposable target bound to a scenario."""
        project, bucket = _parse_bucket_path(scenario.infrastructure.path)
        inspection = self.resolver.inspect(source)
        credential_ref = f"infra/{new_id('lease')}"
        self.resolver.register(credential_ref, source)
        with self.resolver.resolve(credential_ref, inspection.principal) as lease:
            terraform_root = scenario.infrastructure.terraform_root
            if terraform_root is None:
                self.provider.create_bucket(
                    project=project,
                    bucket=bucket,
                    location=location,
                    starting_principal=scenario.starting_service_account.identity,
                    access_token=lease.access_token,
                )
                provisioner = "gcp-api"
            else:
                module_directory = _terraform_root(
                    scenario_directory,
                    terraform_root,
                )
                self.terraform_provisioner.provision(
                    module_directory=module_directory,
                    state_directory=self._terraform_state_directory(scenario),
                    project=project,
                    bucket=bucket,
                    location=location,
                    starting_principal=scenario.starting_service_account.identity,
                    provisioner_member=_iam_member(inspection.principal),
                    access_token=lease.access_token,
                )
                provisioner = "terraform"
        infrastructure = DevelopmentInfrastructure(
            infrastructure_id=new_id("infra"),
            path=scenario.infrastructure.path,
            project=project,
            bucket=bucket,
            location=location,
            starting_principal=scenario.starting_service_account.identity,
            created_by=inspection.principal,
            provisioner=provisioner,
        )
        self.repository.put(infrastructure)
        return infrastructure

    def _terraform_state_directory(self, scenario: PlannerScenario) -> Path:
        identity = "\n".join(
            (
                scenario.name,
                scenario.infrastructure.path,
                scenario.starting_service_account.identity,
            )
        )
        deployment_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.repository.store.directory / "terraform" / deployment_key


def _parse_bucket_path(path: str) -> tuple[str, str]:
    parts = path.split("/")
    if (
        len(parts) != 4
        or parts[0] != "projects"
        or parts[2] != "buckets"
        or _PROJECT_PATTERN.fullmatch(parts[1]) is None
        or _BUCKET_PATTERN.fullmatch(parts[3]) is None
    ):
        raise InfrastructureError(
            "development infrastructure path must be projects/PROJECT/buckets/BUCKET"
        )
    return parts[1], parts[3]


def _terraform_root(scenario_directory: Path | None, relative_root: str) -> Path:
    if scenario_directory is None:
        raise InfrastructureError("scenario directory is required for Terraform infrastructure")
    scenario_root = scenario_directory.resolve()
    module_directory = (scenario_root / relative_root).resolve()
    if not module_directory.is_relative_to(scenario_root):
        raise InfrastructureError("scenario Terraform root escapes the scenario directory")
    return module_directory


def _iam_member(principal: str) -> str:
    if principal.endswith(".iam.gserviceaccount.com"):
        return f"serviceAccount:{principal}"
    if "@" in principal and not any(character.isspace() for character in principal):
        return f"user:{principal}"
    raise InfrastructureError("infrastructure credential principal is not an IAM member")
