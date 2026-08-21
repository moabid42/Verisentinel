"""Parser: scenario file -> validated scenario -> knowledge plane.

Diagram box 1->2: parse the file for env knowledge, detections, techniques, and the solution,
then build the immutable permission-indexed matrix the proposer and validator consume.
"""

from __future__ import annotations

from pathlib import Path

from evaluation_pipeline.knowledge import Knowledge, build_from_iamouflage, build_knowledge
from evaluation_pipeline.scenario import EvaluationScenario, load_scenario


def parse(scenario_path: Path, artifacts_directory: Path) -> Knowledge:
    scenario: EvaluationScenario = load_scenario(scenario_path)
    if scenario.uses_iamouflage:
        return build_from_iamouflage(scenario, artifacts_directory)
    return build_knowledge(scenario, artifacts_directory)
