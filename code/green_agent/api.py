import os

from fastapi import FastAPI, Header, HTTPException

from core.config import Paths
from core.errors import (
    AuthorizationError,
    DataConsistencyError,
    NotFoundError,
    VersionConflictError,
)
from core.models import CreateEngagementRequest, OperatorDecision
from core.security import verify_control_token
from execution.service import ExecutionService
from green_agent.gateways import ExecutionHTTPGateway, LaunchpadHTTPPublisher
from green_agent.orchestrator import GreenAgent

app = FastAPI(title="IAM Green Agent", version="0.1.0")
control_token = os.getenv("CONTROL_PLANE_TOKEN")
paths = Paths()
launchpad = (
    LaunchpadHTTPPublisher(
        base_url=os.environ["LAUNCHPAD_URL"], control_token=control_token
    )
    if os.getenv("LAUNCHPAD_URL")
    else None
)
execution = (
    ExecutionHTTPGateway(
        base_url=os.environ["EXECUTION_URL"], control_token=control_token
    )
    if os.getenv("EXECUTION_URL")
    else ExecutionService(paths=paths)
)
service = GreenAgent(paths=paths, launchpad=launchpad, execution=execution)


def _require_control_token(supplied: str | None) -> None:
    try:
        verify_control_token(control_token, supplied)
    except AuthorizationError as error:
        status = 503 if not control_token else 401
        raise HTTPException(status_code=status, detail=str(error)) from error


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, NotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, VersionConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, AuthorizationError):
        return HTTPException(status_code=403, detail=str(error))
    return HTTPException(status_code=422, detail=str(error))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/engagements")
def create_engagement(
    request: CreateEngagementRequest,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    try:
        return service.create(request)
    except (NotFoundError, DataConsistencyError, AuthorizationError) as error:
        raise _translate(error) from error


@app.get("/engagements/{engagement_id}")
def get_engagement(
    engagement_id: str,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    try:
        return service.get(engagement_id)
    except NotFoundError as error:
        raise _translate(error) from error


@app.post("/engagements/{engagement_id}/cycle")
def start_cycle(
    engagement_id: str,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    try:
        return service.cycle(engagement_id)
    except (
        NotFoundError,
        DataConsistencyError,
        VersionConflictError,
        AuthorizationError,
    ) as error:
        raise _translate(error) from error


@app.post("/engagements/{engagement_id}/decision")
def decide(
    engagement_id: str,
    decision: OperatorDecision,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    if decision.engagement_id != engagement_id:
        raise HTTPException(status_code=422, detail="engagement ID does not match URL")
    try:
        return service.decide(decision)
    except (
        NotFoundError,
        DataConsistencyError,
        VersionConflictError,
        AuthorizationError,
    ) as error:
        raise _translate(error) from error
