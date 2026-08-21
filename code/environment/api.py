from fastapi import FastAPI, HTTPException, Query

from core.errors import DataConsistencyError, NotFoundError, VersionConflictError
from environment.brain import EnvironmentBrain
from environment.models import ApplyObservationRequest, InitializeEnvironmentRequest

app = FastAPI(title="IAM Environment Brain", version="0.1.0")
service = EnvironmentBrain()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/engagements")
def initialize_environment(request: InitializeEnvironmentRequest):
    try:
        return service.initialize(request)
    except (DataConsistencyError, NotFoundError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/engagements/{engagement_id}/state")
def current_state(engagement_id: str):
    try:
        return service.current(engagement_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/engagements/{engagement_id}/state-vector")
def state_vector(
    engagement_id: str,
    identity: str = Query(min_length=1),
    scope: str | None = None,
):
    try:
        return service.state_vector(engagement_id, identity, scope)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DataConsistencyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/engagements/{engagement_id}/observations")
def apply_observation(engagement_id: str, request: ApplyObservationRequest):
    if request.observation.engagement_id != engagement_id:
        raise HTTPException(status_code=422, detail="engagement ID does not match URL")
    try:
        return service.apply_observation(request)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except VersionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except DataConsistencyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

