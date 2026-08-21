from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from evaluation_pipeline.runner import (
    PIPELINE_DIRECTORY,
    RunConfig,
    run_batch,
    run_evaluation,
)
from evaluation_pipeline.scenario import ScenarioError

CODE_DIRECTORY = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evaluation_pipeline",
        description="Evaluate the proposer + validator against a ground-truth scenario solution",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--scenario", type=Path, help="path to a single scenario YAML")
    target.add_argument(
        "--all",
        action="store_true",
        help="run every scenario under --scenarios-dir as one batch",
    )
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=None,
        help="directory that --all scans for *.yaml (default: <pipeline>/scenarios)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use the scripted oracle proposer instead of a live Gemini call",
    )
    parser.add_argument(
        "--quiet-trace",
        action="store_true",
        help="write JSONL diagnostics without echoing event names to stderr",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=None,
        help="override the directory that run outputs are written under",
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    load_dotenv(CODE_DIRECTORY / ".env", override=False)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    run_root = options.run_root.expanduser().resolve() if options.run_root else None
    try:
        if options.all:
            scenarios_dir = (
                options.scenarios_dir or (PIPELINE_DIRECTORY / "scenarios")
            ).expanduser().resolve()
            if not scenarios_dir.is_dir():
                print(f"scenarios directory not found: {scenarios_dir}", file=sys.stderr)
                return 2
            scenario_paths = sorted(scenarios_dir.glob("*.yaml"))
            if not scenario_paths:
                print(f"no *.yaml scenarios under {scenarios_dir}", file=sys.stderr)
                return 2
            return run_batch(
                scenario_paths,
                api_key=api_key,
                offline=options.offline,
                quiet_trace=options.quiet_trace,
                run_root=run_root,
            )
        config = RunConfig(
            scenario_path=options.scenario.expanduser().resolve(),
            api_key=api_key,
            offline=options.offline,
            quiet_trace=options.quiet_trace,
            run_root=run_root,
        )
        return run_evaluation(config)
    except ScenarioError as error:
        print(f"Scenario error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nEvaluation interrupted.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"\nEvaluation failed: {error}", file=sys.stderr)
        return 1
