"""Small provider-neutral contracts for persistent coding sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CopilotTurn:
    """Bounded outcome from one persistent harness turn."""

    response: str
    finish_reason: str | None = None


class CopilotSession(Protocol):
    """One resumable coding conversation with an isolated workspace."""

    @property
    def session_id(self) -> str: ...

    def run(self, prompt: str) -> CopilotTurn: ...


class CopilotHarness(Protocol):
    """Lifecycle boundary implemented by an optional coding harness."""

    @property
    def model(self) -> str: ...

    def require_ready(self) -> None: ...

    def session(self, session_id: str) -> CopilotSession: ...

    def close(self) -> None: ...
