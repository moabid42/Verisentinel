"""Scenario schema for the proposer/validator evaluation pipeline.

A scenario is fully self-contained: it declares the technique catalog, the loaded
detection coverage, the starting identity/permissions, and a ground-truth ``solution``
(ordered commands + scripted outputs). No IAMouflage export, environment brain, green
agent, or spawned infrastructure is required. The solution's per-step ``output`` is the
scripted environment delta applied when the model's plan follows the expected command,
which is what lets the loop advance without any real execution provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from core.models import ImmutableModel


class ScenarioError(ValueError):
    """A scenario is missing or fails its contract."""


def _normalize_command(value: str) -> str:
    return value.strip().removeprefix("technique:")


class Environment(ImmutableModel):
    """The controlled identity and everything already known about the world."""

    identity: str = Field(min_length=1)
    # Informational: the IAM roles bound to the identity. Permissions are what the
    # validator actually reasons over; roles document how the identity obtained them.
    roles: tuple[str, ...] = ()
    starting_permissions: tuple[str, ...] = ()
    discovered_resources: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()

    @field_validator("starting_permissions")
    @classmethod
    def permissions_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("starting_permissions must be unique")
        return value


class ScenarioTechnique(ImmutableModel):
    """One entry in the catalog the proposer is allowed to choose from."""

    id: str = Field(min_length=1)
    title: str = ""
    tactic: str = ""
    service: str = ""
    required_permissions: tuple[str, ...] = ()
    # Permissions the action would emit; defaults to required_permissions when omitted.
    footprint_permissions: tuple[str, ...] = ()
    grants: tuple[str, ...] = ()

    @property
    def effective_footprint(self) -> tuple[str, ...]:
        return self.footprint_permissions or self.required_permissions


class ScenarioDetection(ImmutableModel):
    """One loaded detection rule; its permissions define coverage for the validator."""

    id: str = Field(min_length=1)
    title: str = ""
    source: str = "custom"
    paradigm: str = "event"
    permissions: tuple[str, ...] = Field(min_length=1)


class StepOutput(ImmutableModel):
    """The scripted environment delta applied after a followed step."""

    gained_permissions: tuple[str, ...] = ()
    discovered_resources: tuple[str, ...] = ()
    gained_capabilities: tuple[str, ...] = ()
    note: str = ""


class SolutionStep(ImmutableModel):
    """One ground-truth step: the expected technique and its scripted result."""

    command: str = Field(min_length=1)
    description: str = ""
    output: StepOutput = Field(default_factory=StepOutput)

    @field_validator("command")
    @classmethod
    def strip_prefix(cls, value: str) -> str:
        normalized = _normalize_command(value)
        if not normalized:
            raise ValueError("solution command cannot be empty")
        return normalized


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


class EvaluationSettings(ImmutableModel):
    # top_ranked: the #1 admissible plan must equal the expected command.
    # within_candidates: the expected command must appear among the published plans.
    match_policy: Literal["top_ranked", "within_candidates"] = "top_ranked"
    maximum_candidates: int = Field(default=3, ge=1, le=10)
    maximum_proposals: int = Field(default=10, ge=1, le=50)
    # Bounded proposer<->validator agentic rounds: on rejection the reasons are fed back and
    # the model re-proposes, until `maximum_candidates` accepted plans exist or rounds run out.
    maximum_rounds: int = Field(default=3, ge=1, le=10)
    maximum_steps: int = Field(default=20, ge=1, le=200)
    stop_on_deviation: bool = True
    require_admissible: bool = True


class EvaluationScenario(ImmutableModel):
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    target_scope: str = Field(min_length=1)
    environment: Environment
    # Mode A (inline): the scenario supplies its own catalog and detections; the model is handed
    # these candidates directly. Mode B (IAMouflage): both are empty and `detection_provider`
    # names a corpus (e.g. "sigma"); the real manual is ingested and the proposer retrieves from
    # it with tools. Exactly one mode is active per scenario (see `uses_iamouflage`).
    techniques: tuple[ScenarioTechnique, ...] = ()
    detections: tuple[ScenarioDetection, ...] = ()
    detection_provider: str | None = None
    solution: tuple[SolutionStep, ...] = Field(min_length=1)
    model: ModelSettings = Field(default_factory=ModelSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)

    @property
    def uses_iamouflage(self) -> bool:
        """Mode B: no inline catalog, so techniques + detections come from IAMouflage."""
        return not self.techniques

    @field_validator("target_scope")
    @classmethod
    def target_scope_is_explicit(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith(("projects/", "folders/", "organizations/")):
            raise ValueError("target_scope must start with projects/, folders/, or organizations/")
        return normalized

    @model_validator(mode="after")
    def cross_reference(self) -> EvaluationScenario:
        technique_ids = [technique.id for technique in self.techniques]
        if len(technique_ids) != len(set(technique_ids)):
            raise ValueError("technique ids must be unique")
        detection_ids = [detection.id for detection in self.detections]
        if len(detection_ids) != len(set(detection_ids)):
            raise ValueError("detection ids must be unique")
        if self.uses_iamouflage:
            # Mode B: the catalog is external, so solution commands are checked against the
            # ingested IAMouflage matrix at build time, not here.
            if not self.detection_provider:
                raise ValueError(
                    "IAMouflage mode (no inline techniques) requires a detection_provider "
                    "(e.g. detection_provider: sigma)"
                )
            if self.detections:
                raise ValueError(
                    "inline detections are not allowed in IAMouflage mode; they are sourced "
                    "from detection_provider instead"
                )
        else:
            if self.detection_provider:
                raise ValueError(
                    "detection_provider applies only to IAMouflage mode; it cannot be combined "
                    "with an inline technique catalog"
                )
            catalog = set(technique_ids)
            unknown = [step.command for step in self.solution if step.command not in catalog]
            if unknown:
                raise ValueError(
                    "solution references techniques absent from the catalog: " + ", ".join(unknown)
                )
        return self


def load_scenario(path: Path) -> EvaluationScenario:
    if not path.is_file():
        raise ScenarioError(f"scenario file does not exist: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ScenarioError("scenario is not valid YAML") from error
    if not isinstance(document, dict):
        raise ScenarioError("scenario must be a YAML mapping")
    try:
        return EvaluationScenario.model_validate(document)
    except ValidationError as error:
        messages = [
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors(include_input=False, include_url=False)
        ]
        raise ScenarioError("invalid scenario: " + "; ".join(messages)) from error
