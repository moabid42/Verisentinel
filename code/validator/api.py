from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.errors import PlannerError, http_status_for
from core.models import CandidateValidationRequest, StateValidationRequest
from validator.service import ValidatorService

app = FastAPI(title="IAM Boolean Validator", version="0.1.0")
service = ValidatorService()


@app.exception_handler(PlannerError)
def handle_planner_error(request: Request, error: PlannerError) -> JSONResponse:
    return JSONResponse(status_code=http_status_for(error), content={"detail": str(error)})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": service.engine}


@app.post("/validate/state")
def validate_state(request: StateValidationRequest):
    return service.validate_state(request)


@app.post("/validate/candidate")
def validate_candidate(request: CandidateValidationRequest):
    return service.validate_candidate(request)
