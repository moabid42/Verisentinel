from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, field_validator

from core.models import CREDENTIAL_REFERENCE_PATTERN, ImmutableModel


class ScenarioError(ValueError):
    """A scenario is missing or invalid without exposing its secret values."""


_SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$"
)


class InfrastructureTarget(ImmutableModel):
    """Remote infrastructure selected by one scenario."""

    path: str = Field(min_length=1, max_length=512)

    @field_validator("path")
    @classmethod
    def path_is_canonical(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parts = normalized.split("/")
        if not normalized.startswith("projects/"):
            raise ValueError("path must start with projects/")
        if "//" in normalized or any(part in {".", ".."} for part in parts):
            raise ValueError("path must be a canonical GCP resource name")
        return normalized


class StartingServiceAccount(ImmutableModel):
    identity: str = Field(min_length=1)
    credential_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=CREDENTIAL_REFERENCE_PATTERN,
    )
    permissions: tuple[str, ...] = Field(min_length=1)

    @field_validator("identity")
    @classmethod
    def identity_is_service_account(cls, value: str) -> str:
        normalized = value.strip()
        if _SERVICE_ACCOUNT_PATTERN.fullmatch(normalized) is None:
            raise ValueError("identity must be a service account principal")
        return normalized

    @field_validator("permissions")
    @classmethod
    def permissions_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not permission.strip() for permission in value):
            raise ValueError("permissions cannot contain empty values")
        if len(value) != len(set(value)):
            raise ValueError("permissions must be unique")
        return value


class DetectionProfile(ImmutableModel):
    """The exact detection corpus used to score one scenario."""

    sources: tuple[Literal["sigma", "elastic", "gsecops", "panther", "custom"], ...] = ()
    detection_ids: tuple[str, ...] = ()
    strict: bool = True

    @field_validator("sources", "detection_ids")
    @classmethod
    def selections_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("detection selections must be unique")
        return value


class ModelSettings(ImmutableModel):
    name: str | None = None
    fallback_models: tuple[str, ...] | None = None
    api_mode: Literal["generate_content", "interactions"] | None = None
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    maximum_attempts: int | None = Field(default=None, ge=1, le=10)
    heartbeat_seconds: float | None = Field(default=None, gt=0, le=60)

    @field_validator("fallback_models")
    @classmethod
    def fallback_models_are_unique(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("fallback model names must be unique")
        return value


class PlannerScenario(ImmutableModel):
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    target_scope: str = Field(min_length=1)
    infrastructure: InfrastructureTarget
    starting_service_account: StartingServiceAccount
    detections: DetectionProfile = Field(default_factory=DetectionProfile)
    model: ModelSettings = Field(default_factory=ModelSettings)

    @field_validator("target_scope")
    @classmethod
    def target_scope_is_explicit(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith(("projects/", "folders/", "organizations/")):
            raise ValueError("target_scope must start with projects/, folders/, or organizations/")
        return normalized


def load_scenario(path: Path) -> PlannerScenario:
    if not path.is_file():
        raise ScenarioError(f"scenario file does not exist: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ScenarioError("scenario.yaml contains invalid YAML") from error
    if not isinstance(document, dict):
        raise ScenarioError("scenario.yaml must contain a YAML mapping")
    try:
        return PlannerScenario.model_validate(document)
    except ValidationError as error:
        messages = [
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors(include_input=False, include_url=False)
        ]
        raise ScenarioError("invalid scenario: " + "; ".join(messages)) from error
