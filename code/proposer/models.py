from __future__ import annotations

from pydantic import Field

from core.models import StrictModel


class GeminiChoice(StrictModel):
    technique_id: str
    rationale: str


class GeminiChoices(StrictModel):
    decision_summary: str
    choices: list[GeminiChoice] = Field(default_factory=list)
