import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from core.errors import (
    AuthorizationError,
    DataConsistencyError,
    NotFoundError,
    VersionConflictError,
)
from core.models import OperatorDecision
from core.security import verify_control_token
from launchpad.models import CandidateSet
from launchpad.service import LaunchpadService
from launchpad.sink import GreenAgentDecisionSink
from launchpad.ui import render_dashboard

app = FastAPI(title="IAM Operator Launchpad", version="0.1.0")
control_token = os.getenv("CONTROL_PLANE_TOKEN")
service = LaunchpadService(
    sink=GreenAgentDecisionSink(control_token=control_token)
    if os.getenv("GREEN_AGENT_URL")
    else None
)


def _require_control_token(supplied: str | None) -> None:
    try:
        verify_control_token(control_token, supplied)
    except AuthorizationError as error:
        status = 503 if not control_token else 401
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.put("/engagements/{engagement_id}/candidates")
def publish_candidates(
    engagement_id: str,
    candidate_set: CandidateSet,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    if candidate_set.engagement_id != engagement_id:
        raise HTTPException(status_code=422, detail="engagement ID does not match URL")
    try:
        return service.publish(candidate_set)
    except VersionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DataConsistencyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/engagements/{engagement_id}/candidates")
def candidates(engagement_id: str):
    try:
        return service.candidates(engagement_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/engagements/{engagement_id}", response_class=HTMLResponse)
def dashboard(engagement_id: str):
    try:
        return render_dashboard(service.candidates(engagement_id))
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/engagements/{engagement_id}/decision")
def decide(engagement_id: str, decision: OperatorDecision):
    if decision.engagement_id != engagement_id:
        raise HTTPException(status_code=422, detail="engagement ID does not match URL")
    try:
        return service.decide(decision)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except VersionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DataConsistencyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
