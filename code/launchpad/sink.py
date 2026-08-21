from __future__ import annotations

from typing import Protocol

import httpx

from core.config import ServiceURLs
from core.models import OperatorDecision


class DecisionSink(Protocol):
    def send(self, decision: OperatorDecision) -> None: ...


class GreenAgentDecisionSink:
    def __init__(
        self,
        base_url: str | None = None,
        control_token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (base_url or ServiceURLs().green_agent).rstrip("/")
        self.control_token = control_token
        self.timeout = timeout

    def send(self, decision: OperatorDecision) -> None:
        headers = {"X-Control-Token": self.control_token} if self.control_token else {}
        response = httpx.post(
            f"{self.base_url}/engagements/{decision.engagement_id}/decision",
            json=decision.model_dump(mode="json"),
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()

