import os

from fastapi import FastAPI, Header, HTTPException

from core.errors import AuthorizationError, DataConsistencyError, NotFoundError
from core.models import ApprovalRecord, ExecutionRequest
from core.security import verify_control_token
from execution.models import EngagementAuthorization
from execution.service import ExecutionService

app = FastAPI(title="IAM Guarded Execution Service", version="0.1.0")
control_token = os.getenv("CONTROL_PLANE_TOKEN")
service = ExecutionService()


def _require_control_token(supplied: str | None) -> None:
    try:
        verify_control_token(control_token, supplied)
    except AuthorizationError as error:
        status = 503 if not control_token else 401
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "provider": service.provider, "enabled": service.enabled}


@app.put("/engagements/{engagement_id}/authorization")
def authorize_engagement(
    engagement_id: str,
    authorization: EngagementAuthorization,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    if authorization.engagement_id != engagement_id:
        raise HTTPException(status_code=422, detail="engagement ID does not match URL")
    return service.authorize(authorization)


@app.post("/approvals")
def register_approval(
    approval: ApprovalRecord,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    try:
        return service.register_approval(approval)
    except DataConsistencyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/execute")
def execute(
    request: ExecutionRequest,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    try:
        return service.execute(request)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@app.get("/executions/{execution_id}")
def get_execution(
    execution_id: str,
    x_control_token: str | None = Header(default=None),
):
    _require_control_token(x_control_token)
    try:
        return service.get(execution_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
