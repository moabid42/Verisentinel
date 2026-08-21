from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.errors import PlannerError, http_status_for
from core.models import ProposalRequest
from proposer.service import ProposerService

app = FastAPI(title="IAM Technique Proposer", version="0.1.0")
service = ProposerService()


@app.exception_handler(PlannerError)
def handle_planner_error(request: Request, error: PlannerError) -> JSONResponse:
    return JSONResponse(status_code=http_status_for(error), content={"detail": str(error)})


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "gemini_configured": service.gemini.configured,
        "model": service.gemini.model,
    }


@app.post("/proposals")
def proposals(request: ProposalRequest):
    return service.propose(request)
