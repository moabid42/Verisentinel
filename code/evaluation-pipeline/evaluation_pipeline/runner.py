"""Run entry: set up an isolated run directory, wire the real proposer + validator, drive
the orchestrator, and write a scored report. No spawned infrastructure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from core.tracing import DebugTrace
from evaluation_pipeline.orchestrator import EvaluationOutcome, Orchestrator
from evaluation_pipeline.parser import parse
from evaluation_pipeline.scenario import EvaluationScenario
from proposer.gemini import GeminiProposer
from proposer.service import ProposerService
from validator.service import ValidatorService

PIPELINE_DIRECTORY = Path(__file__).resolve().parents[1]

_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class RunConfig:
    scenario_path: Path
    api_key: str = ""
    offline: bool = False
    quiet_trace: bool = False
    run_root: Path | None = None
    # Explicit run directory. When set it is used verbatim (the batch runner passes
    # `<batch>/<scenario-name>`); otherwise the run directory is `<run_root>/<timestamp>_<name>`.
    run_directory: Path | None = None
    # Test/offline injection: any object with .propose/.model/.last_model.
    proposer_backend: Any | None = None


def _slugify(value: str) -> str:
    """Filesystem-safe folder label derived from a scenario name."""
    slug = _SLUG_PATTERN.sub("-", value.strip()).strip("-._")
    return slug or "scenario"


def _peek_scenario_name(path: Path, fallback: str) -> str:
    """Read just the scenario `name` for the folder label, before full validation.

    Tolerant on purpose: a malformed scenario still gets a run directory (labelled with the
    fallback) so the parse failure can be recorded there rather than crashing before any output.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return _slugify(fallback)
    if isinstance(document, dict):
        name = document.get("name")
        if isinstance(name, str) and name.strip():
            return _slugify(name)
    return _slugify(fallback)


def _unique_directory(base: Path, label: str) -> Path:
    """`base/label`, suffixed with -2, -3, ... only if that path already exists."""
    candidate = base / label
    if not candidate.exists():
        return candidate
    suffix = 2
    while (base / f"{label}-{suffix}").exists():
        suffix += 1
    return base / f"{label}-{suffix}"


def run_evaluation(config: RunConfig) -> int:
    from core.ids import new_id

    run_id = new_id("evaluation")
    # The batch runner supplies an explicit `<batch>/<scenario-name>` directory. Otherwise name
    # the run `<timestamp>_<scenario-name>` so `ls`/globbing sorts chronologically and each folder
    # is recognisable at a glance; the canonical run_id still identifies the run inside the report.
    if config.run_directory is not None:
        run_directory = config.run_directory
    else:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        label = f"{timestamp}_{_peek_scenario_name(config.scenario_path, run_id)}"
        base = config.run_root or (PIPELINE_DIRECTORY / "runs")
        run_directory = _unique_directory(base, label)
    artifacts_directory = run_directory / "artifacts"
    run_directory.mkdir(parents=True, exist_ok=True)

    trace_path = run_directory / "runtime" / "trace.jsonl"
    progress_path = run_directory / "progress.log"
    conversation_path = run_directory / "model-conversation.jsonl"
    state_path = run_directory / "run-state.json"

    secrets = (config.api_key,) if config.api_key else ()
    trace = DebugTrace(
        trace_path,
        run_id,
        secrets=secrets,
        echo=not config.quiet_trace,
        progress_path=progress_path,
    )
    conversation_trace = DebugTrace(conversation_path, run_id, secrets=secrets, echo=False)
    conversation_trace.emit(
        "conversation",
        "log_initialized",
        note="System, user, assistant, thought-summary, usage, and error records follow.",
    )

    _write_run_state(state_path, status="running", run_id=run_id, phase="parsing")
    try:
        knowledge = parse(config.scenario_path, artifacts_directory)
    except BaseException as error:
        _write_run_state(
            state_path,
            status="failed",
            run_id=run_id,
            phase="parsing",
            error_type=type(error).__name__,
            error=str(error),
        )
        trace.emit("evaluation", "parse_failed", level="error", error=str(error))
        raise

    scenario = knowledge.scenario
    trace.emit(
        "evaluation",
        "parsed",
        scenario=scenario.name,
        matrix_version=knowledge.matrix_version,
        permission_count=len(knowledge.matrix.permissions),
        technique_count=len(knowledge.matrix.techniques),
        detection_count=len(knowledge.matrix.detections),
        coverage_permissions=len(knowledge.matrix.coverage_indices),
        total_steps=len(scenario.solution),
    )

    backend = config.proposer_backend or _build_backend(scenario, config, trace, conversation_trace)
    # Offline runs use the scripted oracle, which can only float a solution command it actually
    # sees; give it the full IAMouflage catalog instead of the default top-N prompt slice.
    retriever = None
    if config.offline:
        from proposer.retrieval import TechniqueRetriever

        retriever = TechniqueRetriever(limit=max(50, len(knowledge.matrix.techniques)))
    # Live IAMouflage runs are agentic: the model retrieves from the manual with tools instead of
    # receiving a candidate slice. Offline runs keep the scripted oracle over the full catalog.
    agentic = scenario.uses_iamouflage and not config.offline
    proposer = ProposerService(
        snapshots=knowledge.snapshots, gemini=backend, retriever=retriever, agentic=agentic
    )
    if agentic:
        trace.emit(
            "evaluation",
            "agentic_proposer_enabled",
            detection_provider=scenario.detection_provider,
        )
    validator = ValidatorService(snapshots=knowledge.snapshots)
    orchestrator = Orchestrator(knowledge, proposer, validator, trace=trace)

    _write_run_state(state_path, status="running", run_id=run_id, phase="evaluating")
    try:
        outcome = orchestrator.run()
    except BaseException as error:
        _write_run_state(
            state_path,
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            run_id=run_id,
            phase="evaluating",
            error_type=type(error).__name__,
            error=str(error),
        )
        trace.emit("evaluation", "failed", level="error", error=str(error))
        raise

    report = _report(run_id, scenario, knowledge, outcome, backend)
    report_path = _write_report(run_directory, report)
    # plan.md is a nice-to-have runbook of the validated path; never let it fail the run.
    try:
        plan_path = _write_plan(run_directory, scenario, knowledge, outcome)
    except BaseException as error:  # noqa: BLE001 - artifact writing must not abort a finished run
        trace.emit("evaluation", "plan_write_failed", level="warning", error=str(error))
        plan_path = None
    _write_run_state(
        state_path,
        status="completed",
        run_id=run_id,
        phase="finished",
        passed=outcome.passed,
        report=str(report_path),
    )

    print(f"\nEvaluation run: {run_id}")
    print(f"Scenario: {scenario.name}")
    print(f"Model: {report['model']}")
    print(f"Steps followed: {outcome.steps_followed}/{outcome.total_steps}")
    print(f"Result: {'PASS' if outcome.passed else 'FAIL'}")
    print(f"Report: {report_path}")
    if plan_path is not None:
        print(f"Plan: {plan_path}")
    print(f"Model conversation: {conversation_path}")
    print(f"Progress log: {progress_path}")
    return 0 if outcome.passed else 1


def run_batch(
    scenario_paths: Sequence[Path],
    *,
    api_key: str = "",
    offline: bool = False,
    quiet_trace: bool = False,
    run_root: Path | None = None,
) -> int:
    """Run every scenario into one `<timestamp>_batch_<id>/` folder, one child per scenario.

    Each child directory is named after the scenario's yaml `name`, and a failing (or crashing)
    scenario is recorded and the batch continues. Returns 0 only if every scenario passed.
    """
    from core.ids import new_id

    if not scenario_paths:
        raise RuntimeError("no scenarios to run")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    batch_id = new_id("batch")
    base = run_root or (PIPELINE_DIRECTORY / "runs")
    batch_directory = _unique_directory(base, f"{timestamp}_{batch_id}")
    batch_directory.mkdir(parents=True, exist_ok=True)

    print(f"\nBatch: {batch_id}")
    print(f"Scenarios: {len(scenario_paths)}")
    print(f"Batch directory: {batch_directory}")

    results: list[dict[str, Any]] = []
    for index, scenario_path in enumerate(scenario_paths, start=1):
        name = _peek_scenario_name(scenario_path, scenario_path.stem)
        child = _unique_directory(batch_directory, name)
        print(f"\n[{index}/{len(scenario_paths)}] {name}  ({scenario_path.name})")
        config = RunConfig(
            scenario_path=scenario_path,
            api_key=api_key,
            offline=offline,
            quiet_trace=quiet_trace,
            run_directory=child,
        )
        entry: dict[str, Any] = {
            "scenario": name,
            "path": str(scenario_path),
            "directory": child.name,
        }
        try:
            code = run_evaluation(config)
            entry["exit_code"] = code
            entry["status"] = "pass" if code == 0 else "fail"
        except KeyboardInterrupt:
            raise
        except BaseException as error:  # record and keep going to the next scenario
            entry["exit_code"] = 1
            entry["status"] = "error"
            entry["error"] = f"{type(error).__name__}: {error}"
            print(f"    ERROR: {entry['error']}")
        results.append(entry)

    passed = sum(1 for entry in results if entry["status"] == "pass")
    summary = {
        "batch_id": batch_id,
        "scenario_count": len(scenario_paths),
        "passed": passed,
        "results": results,
    }
    summary_path = batch_directory / "batch-summary.json"
    temporary = summary_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(summary_path)

    marks = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}
    print("\n==== Batch summary ====")
    for entry in results:
        print(f"  {marks[entry['status']]}  {entry['scenario']}")
    print(f"\n{passed}/{len(scenario_paths)} passed")
    print(f"Summary: {summary_path}")
    return 0 if passed == len(scenario_paths) else 1


def _build_backend(
    scenario: EvaluationScenario,
    config: RunConfig,
    trace: DebugTrace,
    conversation_trace: DebugTrace,
) -> Any:
    if config.offline:
        from evaluation_pipeline.scripted_proposer import ScriptedProposer

        oracle = tuple(step.command for step in scenario.solution)
        trace.emit("evaluation", "offline_backend", oracle=list(oracle))
        return ScriptedProposer(oracle=oracle)
    if not config.api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is required for a live run; add it to code/.env or pass --offline"
        )
    return GeminiProposer(
        api_key=config.api_key,
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


def _report(
    run_id: str,
    scenario: EvaluationScenario,
    knowledge,
    outcome: EvaluationOutcome,
    backend: Any,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "scenario": scenario.name,
        "objective": scenario.objective,
        "engagement_id": outcome.engagement_id,
        "passed": outcome.passed,
        "model": getattr(backend, "last_model", getattr(backend, "model", "unknown")),
        "match_policy": scenario.evaluation.match_policy,
        "steps_followed": outcome.steps_followed,
        "total_steps": outcome.total_steps,
        "matrix": {
            "matrix_version": knowledge.matrix_version,
            "permission_count": len(knowledge.matrix.permissions),
            "technique_count": len(knowledge.matrix.techniques),
            "detection_count": len(knowledge.matrix.detections),
            "coverage_permission_count": len(knowledge.matrix.coverage_indices),
        },
        "steps": [verdict.to_dict() for verdict in outcome.verdicts],
        "metrics": outcome.metrics.summary() if outcome.metrics is not None else None,
        "final_environment": outcome.final_env,
    }


def _write_plan(
    run_directory: Path,
    scenario: EvaluationScenario,
    knowledge,
    outcome: EvaluationOutcome,
) -> Path:
    """Render the validated attack path (with per-technique runbooks) to ``plan.md``."""
    from core.config import Paths
    from evaluation_pipeline.plan import build_plan
    from ingestion.technique_source import TechniqueSourceResolver

    resolver = TechniqueSourceResolver(Paths().iamouflage_data)
    text = build_plan(scenario, knowledge, outcome, resolver)
    path = run_directory / "plan.md"
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return path


def _write_report(run_directory: Path, report: dict[str, Any]) -> Path:
    json_path = run_directory / "report.json"
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(json_path)

    lines = [
        f"# Evaluation {report['run_id']}",
        "",
        f"Result: **{'PASS' if report['passed'] else 'FAIL'}**",
        "",
        f"Scenario: `{report['scenario']}`",
        f"Model: `{report['model']}`  ·  Match policy: `{report['match_policy']}`",
        f"Steps followed: {report['steps_followed']}/{report['total_steps']}",
        "",
        "## Steps",
        "",
    ]
    for step in report["steps"]:
        mark = "x" if step["followed"] else " "
        lines.append(f"- [{mark}] step {step['step_index']}: expected `{step['expected_command']}`")
        published = (
            ", ".join(f"{p['technique_id']}(#{p['rank']})" for p in step["published_plans"])
            or "none"
        )
        lines.append(f"      published: {published}")
        lines.append(f"      reason: {step['reason']}")
    lines += _metrics_lines(report.get("metrics"))
    lines += ["", "See `report.json` for the validated plan set and per-candidate evidence.", ""]
    (run_directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return json_path


def _metrics_lines(metrics: dict[str, Any] | None) -> list[str]:
    """Render the per-state cost table plus run totals/averages for report.md."""
    if not metrics:
        return []
    lines = [
        "",
        "## Metrics",
        "",
        "| state | rounds | model calls | tool calls | acc | rej | tokens | latency (s) |",
        "| ----: | -----: | ----------: | ---------: | --: | --: | -----: | ----------: |",
    ]
    for state in metrics["per_state"]:
        candidates = state["candidates"]
        lines.append(
            f"| {state['step_index']} | {state['proposal_rounds']} | {state['model_calls']} "
            f"| {state['tool_calls']} | {candidates['accepted']} | {candidates['rejected']} "
            f"| {state['tokens']['total']} | {state['latency_seconds']} |"
        )
    totals = metrics["totals"]
    averages = metrics["averages"]
    lines += [
        "",
        f"Totals: {totals['states']} states · {totals['proposal_rounds']} proposal rounds · "
        f"{totals['model_calls']} model calls · {totals['tool_calls']} tool calls · "
        f"{totals['tokens']['total']} tokens · {totals['latency_seconds']}s",
        f"Per-state averages: {averages['proposal_rounds']} proposal rounds · "
        f"{averages['model_calls']} model calls · {averages['tool_calls']} tool calls · "
        f"{averages['total_tokens']} tokens · {averages['latency_seconds']}s",
    ]
    return lines


def _write_run_state(path: Path, **values: Any) -> None:
    existing: dict[str, Any] = {}
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
    payload = {**existing, **values, "updated_at": datetime.now(UTC).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
