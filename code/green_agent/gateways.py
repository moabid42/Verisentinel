from __future__ import annotations

from typing import Protocol

import httpx

from core.config import ServiceURLs
from core.models import (
    ActionArtifact,
    ActionAuthorRequest,
    ActionCommand,
    ApprovalRecord,
    ExecutionRequest,
    ExecutionResult,
)
from execution.models import EngagementAuthorization
from launchpad.models import CandidateSet


class CandidatePublisher(Protocol):
    def publish(self, candidate_set: CandidateSet) -> CandidateSet: ...


class ActionAuthor(Protocol):
    """Model boundary that may propose, but cannot write, one action file."""

    def author(self, request: ActionAuthorRequest) -> ActionCommand: ...


class ArtifactWriter(Protocol):
    """File boundary invoked only after an explicit artifact approval."""

    def write(
        self,
        engagement_id: str,
        candidate_id: str,
        artifact: ActionArtifact,
    ) -> ActionArtifact: ...


class ExecutionGateway(Protocol):
    def authorize(
        self, authorization: EngagementAuthorization
    ) -> EngagementAuthorization: ...

    def register_approval(self, approval: ApprovalRecord) -> ApprovalRecord: ...

    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class LaunchpadHTTPPublisher:
    def __init__(
        self,
        base_url: str | None = None,
        control_token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (base_url or ServiceURLs().launchpad).rstrip("/")
        self.headers = {"X-Control-Token": control_token} if control_token else {}
        self.timeout = timeout

    def publish(self, candidate_set: CandidateSet) -> CandidateSet:
        response = httpx.put(
            f"{self.base_url}/engagements/{candidate_set.engagement_id}/candidates",
            json=candidate_set.model_dump(mode="json"),
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return CandidateSet.model_validate(response.json())


class ExecutionHTTPGateway:
    def __init__(
        self,
        base_url: str | None = None,
        control_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or ServiceURLs().execution).rstrip("/")
        self.headers = {"X-Control-Token": control_token} if control_token else {}
        self.timeout = timeout

    def authorize(
        self, authorization: EngagementAuthorization
    ) -> EngagementAuthorization:
        response = httpx.put(
            f"{self.base_url}/engagements/{authorization.engagement_id}/authorization",
            json=authorization.model_dump(mode="json"),
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return EngagementAuthorization.model_validate(response.json())

    def register_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        response = httpx.post(
            f"{self.base_url}/approvals",
            json=approval.model_dump(mode="json"),
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return ApprovalRecord.model_validate(response.json())

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        response = httpx.post(
            f"{self.base_url}/execute",
            json=request.model_dump(mode="json"),
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return ExecutionResult.model_validate(response.json())
