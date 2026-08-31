from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import Field, ValidationError, field_validator

from core.models import CREDENTIAL_REFERENCE_PATTERN, ImmutableModel


class ScenarioError(ValueError):
    """A scenario is missing or invalid without exposing its secret values."""


_SERVICE_ACCOUNT_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$")
_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_TERRAFORM_ROOT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class InfrastructureTarget(ImmutableModel):
    """Remote infrastructure selected by one scenario."""

    path: str = Field(min_length=1, max_length=512)
    terraform_root: str | None = Field(default=None, max_length=256)

    @field_validator("path")
    @classmethod
    def path_is_canonical(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parts = normalized.split("/")
        if (
            len(parts) != 4
            or parts[0] != "projects"
            or parts[2] != "buckets"
            or _PROJECT_PATTERN.fullmatch(parts[1]) is None
            or _BUCKET_PATTERN.fullmatch(parts[3]) is None
        ):
            raise ValueError("path must be projects/PROJECT/buckets/BUCKET")
        return normalized

    @field_validator("terraform_root")
    @classmethod
    def terraform_root_is_relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or _TERRAFORM_ROOT_PATTERN.fullmatch(normalized) is None
        ):
            raise ValueError("terraform_root must be a safe relative directory")
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


class CompletionSettings(ImmutableModel):
    """Deterministic evidence required before displaying scenario completion."""

    flag_template: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("flag_template")
    @classmethod
    def flag_template_has_one_approval_identifier(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        if (
            value.count("{approval_id}") != 1
            or not value.startswith("FLAG{")
            or not value.endswith("}")
        ):
            raise ValueError("flag_template must be a FLAG value containing one {approval_id}")
        if "{" in value.removeprefix("FLAG{").replace("{approval_id}", ""):
            raise ValueError("flag_template contains an unsupported placeholder")
        return value


class CopilotSettings(ImmutableModel):
    """Pinned coding-harness route used after technique selection."""

    enabled: bool = False
    model: str | None = Field(default=None, min_length=1, max_length=128)
    api_key_env: str = "GEMINI_API_KEY"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    timeout_seconds: float = Field(default=300.0, gt=0, le=600)
    maximum_repairs: int = Field(default=3, ge=1, le=10)

    @field_validator("api_key_env")
    @classmethod
    def api_key_environment_is_safe(cls, value: str) -> str:
        if _ENVIRONMENT_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("api_key_env must be an uppercase environment name")
        return value

    @field_validator("base_url")
    @classmethod
    def base_url_is_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise ValueError("base_url must be an HTTPS endpoint without credentials")
        return value.rstrip("/") + "/"


class PlannerScenario(ImmutableModel):
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    target_scope: str = Field(min_length=1)
    infrastructure: InfrastructureTarget
    starting_service_account: StartingServiceAccount
    detections: DetectionProfile = Field(default_factory=DetectionProfile)
    model: ModelSettings = Field(default_factory=ModelSettings)
    copilot: CopilotSettings = Field(default_factory=CopilotSettings)
    completion: CompletionSettings = Field(default_factory=CompletionSettings)

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
