"""Disposable GCP infrastructure for development-mode scenario runs."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import Field

from core.ids import new_id
from core.models import ImmutableModel, utc_now
from core.persistence import JsonModelStore
from execution.credentials import CredentialResolver, CredentialSource
from runner.scenario import PlannerScenario

_INFRASTRUCTURE_ID_PATTERN = re.compile(r"^infra_[0-9a-f]{32}$")
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
        resolver: CredentialResolver | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or GcpInfrastructureProvider()
        self.resolver = resolver or CredentialResolver()

    def create(
        self,
        scenario: PlannerScenario,
        source: CredentialSource,
        *,
        location: str,
    ) -> DevelopmentInfrastructure:
        """Create and persist one disposable target bound to a scenario."""
        project, bucket = _parse_bucket_path(scenario.infrastructure.path)
        inspection = self.resolver.inspect(source)
        credential_ref = f"infra/{new_id('lease')}"
        self.resolver.register(credential_ref, source)
        with self.resolver.resolve(credential_ref, inspection.principal) as lease:
            self.provider.create_bucket(
                project=project,
                bucket=bucket,
                location=location,
                starting_principal=scenario.starting_service_account.identity,
                access_token=lease.access_token,
            )
        infrastructure = DevelopmentInfrastructure(
            infrastructure_id=new_id("infra"),
            path=scenario.infrastructure.path,
            project=project,
            bucket=bucket,
            location=location,
            starting_principal=scenario.starting_service_account.identity,
            created_by=inspection.principal,
        )
        self.repository.put(infrastructure)
        return infrastructure


def _parse_bucket_path(path: str) -> tuple[str, str]:
    parts = path.split("/")
    if len(parts) != 4 or parts[0] != "projects" or parts[2] != "buckets":
        raise InfrastructureError(
            "development infrastructure path must be projects/PROJECT/buckets/BUCKET"
        )
    return parts[1], parts[3]
