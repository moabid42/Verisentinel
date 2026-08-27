"""Scenario-bound sandbox connections without persisted credentials."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import Field, model_validator

from core.errors import AuthorizationError, NotFoundError
from core.ids import new_id
from core.models import ImmutableModel, utc_now
from core.persistence import JsonModelStore
from execution.credentials import (
    CredentialResolver,
    CredentialSource,
    CredentialSourceKind,
)
from runner.infrastructure import (
    DevelopmentInfrastructure,
    InfrastructureRepository,
    _parse_bucket_path,
)
from runner.scenario import PlannerScenario

_STORAGE_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1"


class ConnectionMode(StrEnum):
    """How a sandbox target was resolved."""

    DEVELOPMENT = "development"
    REMOTE = "remote"


class SandboxConnection(ImmutableModel):
    """Non-sensitive binding between a scenario identity and infrastructure."""

    connection_id: str = Field(pattern=r"^connection_[0-9a-f]{32}$")
    mode: ConnectionMode
    infrastructure_id: str | None = None
    infrastructure_path: str = Field(min_length=1, max_length=512)
    scenario_name: str = Field(min_length=1, max_length=256)
    principal: str = Field(min_length=1, max_length=320)
    credential_ref: str = Field(min_length=1, max_length=128)
    source_kind: CredentialSourceKind
    connected_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def development_mode_has_infrastructure_id(self) -> SandboxConnection:
        if self.mode == ConnectionMode.DEVELOPMENT and self.infrastructure_id is None:
            raise ValueError("development connection requires an infrastructure ID")
        if self.mode == ConnectionMode.REMOTE and self.infrastructure_id is not None:
            raise ValueError("remote connection cannot contain a development ID")
        return self


class ConnectionProbe(Protocol):
    """Minimal target access check performed with a short-lived lease."""

    def verify_create_access(
        self,
        *,
        infrastructure_path: str,
        probe_id: str,
        access_token: str,
    ) -> None: ...


class ConnectionActivator(Protocol):
    """Activate the restricted Docker route for a verified connection."""

    def activate(self, *, infrastructure_path: str, principal: str) -> None: ...


class GcpConnectionProbe:
    """Verify object creation against the exact scenario bucket."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def verify_create_access(
        self,
        *,
        infrastructure_path: str,
        probe_id: str,
        access_token: str,
    ) -> None:
        _, bucket = _parse_bucket_path(infrastructure_path)
        object_name = f".verisentinel/connect/{probe_id}"
        try:
            if self._client is None:
                response = httpx.post(
                    f"{_STORAGE_UPLOAD_API}/b/{bucket}/o",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/octet-stream",
                    },
                    params={"uploadType": "media", "name": object_name},
                    content=b"",
                    timeout=20.0,
                )
            else:
                response = self._client.post(
                    f"{_STORAGE_UPLOAD_API}/b/{bucket}/o",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/octet-stream",
                    },
                    params={"uploadType": "media", "name": object_name},
                    content=b"",
                )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise AuthorizationError(
                "credential cannot create objects in the scenario infrastructure"
            ) from error


class SandboxConnectionRepository:
    """Persist the single active connection through atomic replacement."""

    def __init__(self, directory: Path) -> None:
        self.store = JsonModelStore(directory, SandboxConnection)

    def put(self, connection: SandboxConnection) -> None:
        self.store.put("active", connection)

    def active(self) -> SandboxConnection:
        return self.store.get("active")

    def optional_active(self) -> SandboxConnection | None:
        try:
            return self.active()
        except NotFoundError:
            return None


class SandboxConnectionService:
    """Verify and activate scenario-specific sandbox connections."""

    def __init__(
        self,
        repository: SandboxConnectionRepository,
        *,
        infrastructure: InfrastructureRepository | None = None,
        resolver: CredentialResolver | None = None,
        probe: ConnectionProbe | None = None,
        activator: ConnectionActivator | None = None,
    ) -> None:
        self.repository = repository
        self.infrastructure = infrastructure
        self.resolver = resolver or CredentialResolver()
        self.probe = probe or GcpConnectionProbe()
        self.activator = activator

    def connect(
        self,
        scenario: PlannerScenario,
        source: CredentialSource,
        *,
        development_mode: bool,
        infrastructure_id: str | None = None,
    ) -> SandboxConnection:
        """Verify the exact scenario principal and activate its target."""
        development = self._development_target(
            scenario,
            development_mode=development_mode,
            infrastructure_id=infrastructure_id,
        )
        connection_id = new_id("connection")
        credential_ref = scenario.starting_service_account.credential_ref
        self.resolver.register(credential_ref, source)
        with self.resolver.resolve(
            credential_ref,
            scenario.starting_service_account.identity,
        ) as lease:
            self.probe.verify_create_access(
                infrastructure_path=scenario.infrastructure.path,
                probe_id=connection_id,
                access_token=lease.access_token,
            )
        connection = SandboxConnection(
            connection_id=connection_id,
            mode=(ConnectionMode.DEVELOPMENT if development_mode else ConnectionMode.REMOTE),
            infrastructure_id=(development.infrastructure_id if development is not None else None),
            infrastructure_path=scenario.infrastructure.path,
            scenario_name=scenario.name,
            principal=scenario.starting_service_account.identity,
            credential_ref=credential_ref,
            source_kind=source.kind,
        )
        if self.activator is not None:
            self.activator.activate(
                infrastructure_path=connection.infrastructure_path,
                principal=connection.principal,
            )
        self.repository.put(connection)
        return connection

    def _development_target(
        self,
        scenario: PlannerScenario,
        *,
        development_mode: bool,
        infrastructure_id: str | None,
    ) -> DevelopmentInfrastructure | None:
        if not development_mode:
            if infrastructure_id is not None:
                raise ValueError("infrastructure IDs are accepted only with the --dev option")
            return None
        if infrastructure_id is None:
            raise ValueError("development connection requires an infrastructure ID")
        if self.infrastructure is None:
            raise RuntimeError("development infrastructure repository is unavailable")
        target = self.infrastructure.get(infrastructure_id)
        if target.path != scenario.infrastructure.path:
            raise AuthorizationError("development infrastructure does not match the scenario path")
        if target.starting_principal != scenario.starting_service_account.identity:
            raise AuthorizationError(
                "development infrastructure does not match the scenario principal"
            )
        return target
