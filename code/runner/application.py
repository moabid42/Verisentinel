"""Application services used by the command-line boundary."""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from core.config import Paths
from core.errors import NotFoundError
from core.ids import new_id
from core.models import (
    CreateEngagementRequest,
    CycleResult,
    EngagementStatus,
    MatrixSnapshot,
    OperatorDecision,
)
from core.tracing import DebugTrace
from execution.capsule.doctor import CapsuleDoctor, CapsuleDoctorReport
from execution.credentials import (
    CredentialInspection,
    CredentialResolver,
    CredentialSource,
)
from execution.service import ExecutionService
from green_agent.orchestrator import GreenAgent
from ingestion.models import BuildSnapshotRequest
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository
from launchpad.service import LaunchpadService
from proposer.gemini import GeminiProposer
from proposer.service import ProposerService
from runner.scenario import PlannerScenario, load_scenario
from runner.terminal import parse_choice, render_candidates

CODE_DIRECTORY = Path(__file__).resolve().parents[1]


class PlannerConfigurationError(ValueError):
    """The local planner configuration is incomplete or invalid."""


class PlannerRunError(RuntimeError):
    """A planner run failed after its diagnostic trace was initialized."""


class CorpusGateway(Protocol):
    """Operations needed by corpus CLI services."""

    def build(self, request: BuildSnapshotRequest) -> MatrixSnapshot: ...

    def current(self) -> MatrixSnapshot: ...


class CapsuleDoctorGateway(Protocol):
    """Read-only capsule operation needed by sandbox diagnostics."""

    def inspect(self) -> CapsuleDoctorReport: ...


@dataclass(frozen=True, slots=True)
class CorpusStatus:
    """Bounded summary of the current coverage corpus."""

    available: bool
    matrix_version: str | None = None
    permission_count: int = 0
    detection_count: int = 0
    technique_count: int = 0


@dataclass(frozen=True, slots=True)
class PlannerRunRequest:
    """Validated input for one direct planner workflow."""

    scenario_path: Path
    credential_source: CredentialSource
    rebuild_snapshot: bool = False
    quiet_trace: bool = False


def validate_scenario(path: Path) -> PlannerScenario:
    """Load and validate one scenario without starting a planner run."""
    return load_scenario(path.expanduser().resolve())


def inspect_authentication(
    source: CredentialSource,
    *,
    resolver: CredentialResolver | None = None,
) -> CredentialInspection:
    """Resolve a credential source and return only verified public metadata."""
    return (resolver or CredentialResolver()).inspect(source)


def build_corpus(*, service: CorpusGateway | None = None) -> CorpusStatus:
    """Build the default coverage corpus and return its summary."""
    snapshot = (service or IngestorService()).build(BuildSnapshotRequest())
    return _corpus_status(snapshot)


def read_corpus_status(*, service: CorpusGateway | None = None) -> CorpusStatus:
    """Return the current corpus summary without building it."""
    try:
        snapshot = (service or IngestorService()).current()
    except NotFoundError:
        return CorpusStatus(available=False)
    return _corpus_status(snapshot)


def inspect_sandbox(
    *,
    doctor: CapsuleDoctorGateway | None = None,
) -> CapsuleDoctorReport:
    """Report local capsule readiness without changing runtime state."""
    return (doctor or CapsuleDoctor()).inspect()


def run_planner(request: PlannerRunRequest) -> int:
    """Run the existing interactive planner workflow through shared services."""
    load_dotenv(CODE_DIRECTORY / ".env", override=False)
    scenario = validate_scenario(request.scenario_path)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PlannerConfigurationError(
            "GEMINI_API_KEY is empty; add it to code/.env before running"
        )

    paths = Paths()
    run_id = new_id("run")
    trace_path = paths.runtime / "traces" / f"{run_id}.jsonl"
    progress_path = paths.runtime / "traces" / f"{run_id}.progress.log"
    conversation_path = paths.runtime / "traces" / (
        f"{run_id}.model-conversation.jsonl"
    )
    trace = DebugTrace(
        trace_path,
        run_id,
        secrets=(api_key,),
        echo=not request.quiet_trace,
        progress_path=progress_path,
    )
    conversation_trace = DebugTrace(
        conversation_path,
        run_id,
        secrets=(api_key,),
        echo=False,
    )
    conversation_trace.emit(
        "conversation",
        "log_initialized",
        scenario=scenario.name,
        note=(
            "System, user, assistant, thought-summary, usage, and error "
            "records follow."
        ),
    )
    trace.emit(
        "runner",
        "started",
        scenario=scenario.name,
        scenario_path=str(request.scenario_path),
        target_scope=scenario.target_scope,
        identity=scenario.starting_service_account.identity,
        trace_path=str(trace_path),
    )

    try:
        snapshots = SnapshotRepository(paths.artifacts / "snapshots")
        matrix = _ensure_snapshot(
            snapshots,
            paths,
            scenario=scenario,
            rebuild=request.rebuild_snapshot,
            trace=trace,
        )
        gemini = GeminiProposer(
            api_key=api_key,
            model=scenario.model.name,
            trace=trace,
            conversation_trace=conversation_trace,
            timeout_seconds=scenario.model.timeout_seconds,
            maximum_attempts=scenario.model.maximum_attempts,
            thinking_level=scenario.model.thinking_level,
            api_mode=scenario.model.api_mode,
            fallback_models=scenario.model.fallback_models,
            heartbeat_seconds=scenario.model.heartbeat_seconds,
        )
        proposer = ProposerService(
            snapshots=snapshots,
            gemini=gemini,
            paths=paths,
        )
        launchpad = LaunchpadService(paths=paths)
        credential_resolver = CredentialResolver()
        credential_resolver.register(
            scenario.starting_service_account.credential_ref,
            request.credential_source,
        )
        execution = ExecutionService(
            snapshots=snapshots,
            paths=paths,
            credential_resolver=credential_resolver,
            trace=trace,
        )
        planner = GreenAgent(
            snapshots=snapshots,
            proposer=proposer,
            launchpad=launchpad,
            execution=execution,
            paths=paths,
            trace=trace,
        )
        engagement = planner.create(
            CreateEngagementRequest(
                objective=scenario.objective,
                identity=scenario.starting_service_account.identity,
                credential=scenario.starting_service_account.credential_ref,
                target_scope=scenario.target_scope,
                permissions=scenario.starting_service_account.permissions,
                state_source=f"scenario:{scenario.name}",
            )
        )
        trace.emit(
            "runner",
            "engagement_created",
            engagement_id=engagement.engagement_id,
            matrix_version=matrix.matrix_version,
            state_version=engagement.state_version,
            execution_provider=execution.provider,
        )
        print(f"\nEngagement: {engagement.engagement_id}")
        print(f"Execution provider: {execution.provider}")
        print(f"Debug trace: {trace_path}")
        print(f"Model conversation: {conversation_path}")
        result = planner.cycle(engagement.engagement_id)
        return _operator_loop(planner, launchpad, scenario, result, trace)
    except KeyboardInterrupt:
        trace.emit("runner", "interrupted", level="warning")
        raise
    except Exception as error:
        trace.emit(
            "runner",
            "failed",
            level="error",
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        raise PlannerRunError(
            f"planner run failed; inspect the debug trace at {trace_path}"
        ) from error


def _corpus_status(snapshot: MatrixSnapshot) -> CorpusStatus:
    return CorpusStatus(
        available=True,
        matrix_version=snapshot.matrix_version,
        permission_count=len(snapshot.permissions),
        detection_count=len(snapshot.detections),
        technique_count=len(snapshot.techniques),
    )


def _ensure_snapshot(
    snapshots: SnapshotRepository,
    paths: Paths,
    *,
    scenario: PlannerScenario,
    rebuild: bool,
    trace: DebugTrace,
) -> MatrixSnapshot:
    request = BuildSnapshotRequest(
        enabled_detection_ids=scenario.detections.detection_ids,
        enabled_sources=scenario.detections.sources,
        strict=scenario.detections.strict,
    )
    if not rebuild:
        try:
            matrix = snapshots.current()
        except NotFoundError:
            pass
        else:
            configured_sources = set(scenario.detections.sources)
            configured_ids = set(scenario.detections.detection_ids)
            matrix_sources = {row.source for row in matrix.detections.values()}
            if configured_sources and matrix_sources != configured_sources:
                trace.emit(
                    "runner",
                    "snapshot_profile_mismatch",
                    configured_sources=sorted(configured_sources),
                    matrix_sources=sorted(matrix_sources),
                )
            elif configured_ids and set(matrix.enabled_detection_ids) != configured_ids:
                trace.emit(
                    "runner",
                    "snapshot_profile_mismatch",
                    configured_detection_count=len(configured_ids),
                    matrix_detection_count=len(matrix.enabled_detection_ids),
                )
            else:
                trace.emit(
                    "runner",
                    "snapshot_loaded",
                    matrix_version=matrix.matrix_version,
                    technique_count=len(matrix.techniques),
                    permission_count=len(matrix.permissions),
                    detection_count=len(matrix.detections),
                    detection_sources=sorted(matrix_sources),
                )
                return matrix
    trace.emit("runner", "snapshot_build_started")
    matrix = IngestorService(paths=paths, repository=snapshots).build(request)
    trace.emit(
        "runner",
        "snapshot_build_completed",
        matrix_version=matrix.matrix_version,
        technique_count=len(matrix.techniques),
        permission_count=len(matrix.permissions),
    )
    return matrix


def _operator_loop(
    planner: GreenAgent,
    launchpad: LaunchpadService,
    scenario: PlannerScenario,
    result: CycleResult,
    trace: DebugTrace,
) -> int:
    while result.status == EngagementStatus.AWAITING_APPROVAL:
        print(render_candidates(result.candidates))
        print(
            "\nChoose: 1-3 approve | r1-r3 reject | a alternatives | "
            "x reject all | q terminate"
        )
        while True:
            try:
                choice = parse_choice(input("> "), result.candidates)
                break
            except ValueError as error:
                print(f"Invalid choice: {error}")

        current = planner.get(result.engagement_id)
        decision = OperatorDecision(
            engagement_id=current.engagement_id,
            decision=choice.decision,
            candidate_id=choice.candidate_id,
            state_version=current.state_version,
            matrix_version=current.matrix_version,
            operator=scenario.operator,
            reason=choice.reason,
        )
        trace.emit(
            "operator",
            "decision_submitted",
            decision=choice.decision,
            candidate_id=choice.candidate_id,
            operator=scenario.operator,
        )
        launchpad.decide(decision)
        result = planner.decide(decision)
        print(f"\n{result.message}")

    trace.emit(
        "runner",
        "finished",
        engagement_id=result.engagement_id,
        status=result.status,
        message=result.message,
    )
    return 0 if result.status != EngagementStatus.FAILED else 1
