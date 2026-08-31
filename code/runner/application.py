"""Application services used by the command-line boundary."""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from action_agent.harness import HarnessActionAgent
from action_agent.service import ActionAgentService
from copilot.deepseek import DeepSeekCopilotHarness
from core.config import Paths
from core.errors import NotFoundError
from core.ids import new_id
from core.models import (
    CandidateCard,
    CreateEngagementRequest,
    CycleResult,
    DecisionKind,
    EngagementStatus,
    MatrixSnapshot,
    OperatorDecision,
    ReviewStage,
)
from core.tracing import DebugTrace
from environment.repository import EnvironmentRepository
from execution.capsule.doctor import CapsuleDoctor, CapsuleDoctorReport
from execution.capsule.setup import CapsuleBuilder, CapsuleBuildReport
from execution.credentials import (
    CredentialInspection,
    CredentialResolver,
    CredentialSource,
    CredentialSourceKind,
)
from execution.gateway.manager import GatewayBuilder, GatewayBuildReport, GatewayManager
from execution.service import ExecutionService
from green_agent.orchestrator import GreenAgent
from green_agent.repository import GreenAgentRepository
from ingestion.models import BuildSnapshotRequest
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository
from launchpad.service import LaunchpadService
from proposer.gemini import GeminiProposer
from proposer.service import ProposerService
from runner.connection import (
    ConnectionMode,
    SandboxConnection,
    SandboxConnectionRepository,
)
from runner.scenario import PlannerScenario, load_scenario
from runner.terminal import TerminalChoice, TerminalUI, parse_choice

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


class CapsuleBuilderGateway(Protocol):
    """Local capsule setup operation needed by the CLI."""

    def build(self) -> CapsuleBuildReport: ...


class InfrastructureGatewayBuilder(Protocol):
    """Gateway image operation needed by sandbox setup."""

    def build(self) -> GatewayBuildReport: ...


class ActionAssistant(Protocol):
    """Non-authorizing model conversation for a pending action."""

    def answer(self, card: CandidateCard, question: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CorpusStatus:
    """Bounded summary of the current coverage corpus."""

    available: bool
    matrix_version: str | None = None
    permission_count: int = 0
    detection_count: int = 0
    technique_count: int = 0


@dataclass(frozen=True, slots=True)
class ConnectedEnvironment:
    """Non-sensitive current state for the active sandbox identity."""

    connection_id: str
    infrastructure_id: str | None
    scenario_name: str
    objective: str
    identity: str
    target_scope: str
    infrastructure_path: str
    credential_ref: str
    source_kind: str
    permissions: tuple[str, ...]
    detection_sources: tuple[str, ...]
    engagement_id: str | None = None
    engagement_status: str | None = None
    state_version: str | None = None
    discovered_resources: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    completed_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannerRunRequest:
    """Validated input for one direct planner workflow."""

    scenario_path: Path
    credential_source: CredentialSource
    rebuild_snapshot: bool = False
    quiet_trace: bool = False
    development_mode: bool = False


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


def build_sandbox(
    *,
    builder: CapsuleBuilderGateway | None = None,
    gateway_builder: InfrastructureGatewayBuilder | None = None,
) -> CapsuleBuildReport:
    """Build and prepare the local execution capsule."""
    report = (builder or CapsuleBuilder()).build()
    if builder is not None and gateway_builder is None:
        return report
    gateway = (gateway_builder or GatewayBuilder()).build()
    return CapsuleBuildReport(
        image=report.image,
        network=report.network,
        network_created=report.network_created,
        gateway_image=gateway.image,
    )


def read_connected_environment(
    *,
    paths: Paths | None = None,
) -> ConnectedEnvironment:
    """Read scenario and current Environment Brain state for the connection."""
    paths = paths or Paths()
    connection, scenario = _connected_scenario(paths)
    permissions = scenario.starting_service_account.permissions
    engagement_status: str | None = None
    state_version: str | None = None
    discovered_resources: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    completed_actions: tuple[str, ...] = ()
    if connection.engagement_id is not None:
        try:
            environment = EnvironmentRepository(paths.runtime / "environment").current(
                connection.engagement_id
            )
            matrix = SnapshotRepository(paths.artifacts / "snapshots").get(
                environment.matrix_version
            )
            vectors = tuple(
                vector
                for vector in environment.identities.values()
                if vector.identity == connection.principal and vector.scope == scenario.target_scope
            )
            if len(vectors) != 1:
                raise PlannerConfigurationError(
                    "connected identity has no unambiguous environment state"
                )
            permissions = tuple(
                matrix.permissions[index] for index in vectors[0].permission_indices
            )
            state_version = environment.state_version
            discovered_resources = environment.discovered_resources
            capabilities = environment.capabilities
            completed_actions = environment.completed_actions
            engagement_status = (
                GreenAgentRepository(paths.runtime / "green-agent")
                .get(connection.engagement_id)
                .status.value
            )
        except NotFoundError:
            pass
    return ConnectedEnvironment(
        connection_id=connection.connection_id,
        infrastructure_id=connection.infrastructure_id,
        scenario_name=scenario.name,
        objective=scenario.objective,
        identity=connection.principal,
        target_scope=scenario.target_scope,
        infrastructure_path=connection.infrastructure_path,
        credential_ref=connection.credential_ref,
        source_kind=connection.source_kind.value,
        permissions=permissions,
        detection_sources=scenario.detections.sources,
        engagement_id=connection.engagement_id,
        engagement_status=engagement_status,
        state_version=state_version,
        discovered_resources=discovered_resources,
        capabilities=capabilities,
        completed_actions=completed_actions,
    )


def analyse_connected_environment(
    *,
    credential_source: CredentialSource | None = None,
    rebuild_snapshot: bool = False,
    quiet_trace: bool = False,
    paths: Paths | None = None,
) -> int:
    """Run the guarded proposer and validator loop for the active sandbox."""
    paths = paths or Paths()
    connection, _ = _connected_scenario(paths)
    source = credential_source
    if source is None:
        if connection.source_kind != CredentialSourceKind.IMPERSONATE:
            raise PlannerConfigurationError(
                "connected credential source cannot be reconstructed; pass "
                "--credential-source SOURCE"
            )
        source = CredentialSource(
            kind=CredentialSourceKind.IMPERSONATE,
            locator=connection.principal,
        )
    return run_planner(
        PlannerRunRequest(
            scenario_path=Path(connection.scenario_path or ""),
            credential_source=source,
            rebuild_snapshot=rebuild_snapshot,
            quiet_trace=quiet_trace,
            development_mode=connection.mode == ConnectionMode.DEVELOPMENT,
        )
    )


def run_planner(request: PlannerRunRequest) -> int:
    """Run the existing interactive planner workflow through shared services."""
    load_dotenv(CODE_DIRECTORY / ".env", override=False)
    scenario = validate_scenario(request.scenario_path)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PlannerConfigurationError(
            "GEMINI_API_KEY is empty; add it to code/.env before running"
        )
    copilot_api_key = (
        os.getenv(scenario.copilot.api_key_env, "").strip() if scenario.copilot.enabled else ""
    )
    if scenario.copilot.enabled and not copilot_api_key:
        raise PlannerConfigurationError(
            f"{scenario.copilot.api_key_env} is empty; configure the DeepSeek "
            "Harness model credential before running"
        )

    paths = Paths()
    connection = _require_sandbox_connection(
        paths,
        scenario,
        request,
    )
    run_id = new_id("run")
    trace_path = paths.runtime / "traces" / f"{run_id}.jsonl"
    progress_path = paths.runtime / "traces" / f"{run_id}.progress.log"
    conversation_path = paths.runtime / "traces" / (f"{run_id}.model-conversation.jsonl")
    trace = DebugTrace(
        trace_path,
        run_id,
        secrets=tuple(secret for secret in (api_key, copilot_api_key) if secret),
        echo=not request.quiet_trace,
        progress_path=progress_path,
    )
    conversation_trace = DebugTrace(
        conversation_path,
        run_id,
        secrets=tuple(secret for secret in (api_key, copilot_api_key) if secret),
        echo=False,
    )
    conversation_trace.emit(
        "conversation",
        "log_initialized",
        scenario=scenario.name,
        note=("System, user, assistant, thought-summary, usage, and error records follow."),
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

    copilot_harness: DeepSeekCopilotHarness | None = None
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
            provider="capsule",
            enabled=True,
        )
        if scenario.copilot.enabled:
            copilot_root = paths.runtime / "copilot" / run_id
            copilot_harness = DeepSeekCopilotHarness(
                api_key=copilot_api_key,
                model=(scenario.copilot.model or scenario.model.name or "gemini-3.6-flash"),
                workspace_root=copilot_root / "workspace",
                session_root=copilot_root / "sessions",
                base_url=scenario.copilot.base_url,
                timeout_seconds=scenario.copilot.timeout_seconds,
                trace=trace,
            )
            copilot_harness.require_ready()
            action_assistant = HarnessActionAgent(
                copilot_harness,
                copilot_root / "workspace",
                maximum_repairs=scenario.copilot.maximum_repairs,
                trace=trace,
            )
        else:
            action_assistant = ActionAgentService(gemini)
        planner = GreenAgent(
            snapshots=snapshots,
            proposer=proposer,
            launchpad=launchpad,
            execution=execution,
            action_author=action_assistant,
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
        SandboxConnectionRepository(paths.runtime / "sandbox" / "connection").put(
            connection.model_copy(update={"engagement_id": engagement.engagement_id})
        )
        trace.emit(
            "runner",
            "engagement_created",
            engagement_id=engagement.engagement_id,
            matrix_version=matrix.matrix_version,
            state_version=engagement.state_version,
            execution_provider=execution.provider,
            infrastructure_path=connection.infrastructure_path,
        )
        terminal = TerminalUI()
        terminal.run_started(
            engagement_id=engagement.engagement_id,
            provider=execution.provider,
            trace_path=str(trace_path),
            conversation_path=str(conversation_path),
        )
        result = planner.cycle(engagement.engagement_id)
        return _operator_loop(
            planner,
            launchpad,
            action_assistant,
            scenario,
            result,
            trace,
            terminal,
        )
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
    finally:
        if copilot_harness is not None:
            copilot_harness.close()


def _corpus_status(snapshot: MatrixSnapshot) -> CorpusStatus:
    return CorpusStatus(
        available=True,
        matrix_version=snapshot.matrix_version,
        permission_count=len(snapshot.permissions),
        detection_count=len(snapshot.detections),
        technique_count=len(snapshot.techniques),
    )


def _connected_scenario(
    paths: Paths,
) -> tuple[SandboxConnection, PlannerScenario]:
    connection = SandboxConnectionRepository(
        paths.runtime / "sandbox" / "connection"
    ).optional_active()
    if connection is None:
        raise PlannerConfigurationError("sandbox is not connected; run sandbox connect first")
    if connection.scenario_path is None:
        raise PlannerConfigurationError(
            "active connection has no bound scenario; reconnect the sandbox"
        )
    scenario = load_scenario(Path(connection.scenario_path))
    expected = (
        scenario.name,
        scenario.infrastructure.path,
        scenario.starting_service_account.identity,
        scenario.starting_service_account.credential_ref,
    )
    actual = (
        connection.scenario_name,
        connection.infrastructure_path,
        connection.principal,
        connection.credential_ref,
    )
    if actual != expected:
        raise PlannerConfigurationError(
            "active sandbox connection does not match its bound scenario"
        )
    return connection, scenario


def _require_sandbox_connection(
    paths: Paths,
    scenario: PlannerScenario,
    request: PlannerRunRequest,
) -> SandboxConnection:
    connection = SandboxConnectionRepository(
        paths.runtime / "sandbox" / "connection"
    ).optional_active()
    if connection is None:
        raise PlannerConfigurationError(
            "sandbox is not connected; run sandbox connect before the scenario"
        )
    expected_mode = (
        ConnectionMode.DEVELOPMENT if request.development_mode else ConnectionMode.REMOTE
    )
    expected = (
        expected_mode,
        scenario.name,
        scenario.infrastructure.path,
        scenario.starting_service_account.identity,
        scenario.starting_service_account.credential_ref,
        request.credential_source.kind,
    )
    actual = (
        connection.mode,
        connection.scenario_name,
        connection.infrastructure_path,
        connection.principal,
        connection.credential_ref,
        connection.source_kind,
    )
    if actual != expected:
        raise PlannerConfigurationError(
            "active sandbox connection does not match this scenario and credential source"
        )
    GatewayManager().require_active(
        infrastructure_path=connection.infrastructure_path,
        principal=connection.principal,
    )
    return connection


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
    action_assistant: ActionAssistant,
    scenario: PlannerScenario,
    result: CycleResult,
    trace: DebugTrace,
    terminal: TerminalUI,
) -> int:
    while result.status == EngagementStatus.AWAITING_APPROVAL:
        terminal.candidates(
            result.candidates,
            review_stage=result.review_stage,
        )
        while True:
            try:
                choice = parse_choice(
                    terminal.choice_prompt(result.review_stage),
                    result.candidates,
                )
            except ValueError as error:
                terminal.invalid_choice(str(error))
                continue
            if choice.inspection is not None:
                if result.review_stage not in {
                    ReviewStage.ACTION_ARTIFACT,
                    ReviewStage.ACTION_EXECUTION,
                }:
                    terminal.invalid_choice(
                        "source and input inspection are available during action review"
                    )
                    continue
                card = _displayed_candidate(result, choice.candidate_id)
                command = card.action_command
                if command is None:
                    terminal.invalid_choice("the displayed action has no command")
                    continue
                if choice.inspection == "input":
                    if result.review_stage != ReviewStage.ACTION_EXECUTION:
                        terminal.invalid_choice(
                            "exact command input is available after file approval"
                        )
                        continue
                    terminal.command_input(command.input_preview)
                    continue
                artifact = command.artifact
                if artifact is None:
                    terminal.invalid_choice("the displayed action has no model-authored source")
                    continue
                terminal.command_source(
                    reference=artifact.workspace_path or artifact.path,
                    installation=command.tool_installation,
                    digest=artifact.digest,
                    content=artifact.content,
                )
                continue
            if choice.question:
                if result.review_stage not in {
                    ReviewStage.ACTION_ARTIFACT,
                    ReviewStage.ACTION_EXECUTION,
                }:
                    terminal.invalid_choice(
                        "free-form model interaction starts after technique selection"
                    )
                    continue
                if not result.candidates:
                    terminal.invalid_choice("no pending action is available for model interaction")
                    continue
                card = result.candidates[0]
                try:
                    answer = action_assistant.answer(
                        card,
                        choice.question,
                    )
                except (RuntimeError, ValueError) as error:
                    terminal.invalid_choice(str(error))
                    continue
                terminal.agent_message(answer)
                continue
            break

        if choice.decision in {
            DecisionKind.REJECT,
            DecisionKind.REJECT_ALL,
            DecisionKind.REQUEST_ALTERNATIVES,
        }:
            feedback = terminal.feedback_prompt()
            while not feedback:
                terminal.invalid_choice("feedback is required to guide the next proposal")
                feedback = terminal.feedback_prompt()
            choice = TerminalChoice(
                decision=choice.decision,
                candidate_id=choice.candidate_id,
                reason=feedback,
            )

        current = planner.get(result.engagement_id)
        if choice.decision is None:
            raise PlannerConfigurationError("operator choice has no decision")
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
            feedback=choice.reason,
        )
        launchpad.decide(decision)
        result = planner.decide(decision)
        if result.execution_observation is not None:
            completion_evidence = _completion_evidence(scenario, result)
            terminal.command_output(
                result.execution_observation,
                command=result.executed_command,
                state_version=result.resulting_state_version,
                completion_evidence=completion_evidence,
            )
        else:
            terminal.cycle_message(result.message)

    trace.emit(
        "runner",
        "finished",
        engagement_id=result.engagement_id,
        status=result.status,
        message=result.message,
    )
    failed = result.status == EngagementStatus.FAILED
    terminal.finished(failed=failed)
    return 1 if failed else 0


def _displayed_candidate(
    result: CycleResult,
    candidate_id: str | None,
) -> CandidateCard:
    for card in result.candidates:
        if card.proposal.candidate_id == candidate_id:
            return card
    raise PlannerConfigurationError("inspection references an undisplayed candidate")


def _completion_evidence(
    scenario: PlannerScenario,
    result: CycleResult,
) -> str | None:
    """Return a flag only when approved provider evidence matches the preview."""
    template = scenario.completion.flag_template
    command = result.executed_command
    observation = result.execution_observation
    if template is None or command is None or observation is None:
        return None
    if not observation.success or command.approval_id is None:
        return None
    created_resources = {
        effect.removeprefix("Create ")
        for effect in command.side_effects
        if effect.startswith("Create ")
    }
    if not created_resources.intersection(observation.discovered_resources):
        raise PlannerConfigurationError(
            "scenario completion evidence does not match the approved side effect"
        )
    return template.replace("{approval_id}", command.approval_id)
