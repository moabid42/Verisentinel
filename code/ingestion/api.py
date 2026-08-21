from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.errors import PlannerError, http_status_for
from ingestion.models import BuildSnapshotRequest
from ingestion.service import IngestorService

app = FastAPI(title="IAM Coverage Ingestor", version="0.1.0")
service = IngestorService()


@app.exception_handler(PlannerError)
def handle_planner_error(request: Request, error: PlannerError) -> JSONResponse:
    return JSONResponse(status_code=http_status_for(error), content={"detail": str(error)})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/snapshots/build")
def build_snapshot(request: BuildSnapshotRequest):
    return service.build(request)


@app.get("/snapshots/current")
def current_snapshot():
    return service.current()


@app.get("/snapshots/{matrix_version}")
def get_snapshot(matrix_version: str):
    return service.get(matrix_version)


@app.get("/snapshots/{matrix_version}/report")
def get_report(matrix_version: str):
    return service.report(matrix_version)
