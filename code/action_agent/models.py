"""Strict model output contracts for the post-technique action agent."""

from pydantic import Field

from core.models import StrictModel


class AuthoredAction(StrictModel):
    """One complete source file proposed by the model."""

    explanation: str = Field(min_length=1, max_length=2048)
    file_path: str = Field(min_length=1, max_length=128)
    file_content: str = Field(min_length=1, max_length=65_536)
