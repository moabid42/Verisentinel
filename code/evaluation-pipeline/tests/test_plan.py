"""Tests for the validated-path runbook (evaluation_pipeline/plan.py) and its source resolver."""

from __future__ import annotations

import json
from pathlib import Path

from evaluation_pipeline.knowledge import build_knowledge
from evaluation_pipeline.orchestrator import Orchestrator
from evaluation_pipeline.plan import build_plan
from evaluation_pipeline.scenario import EvaluationScenario
from evaluation_pipeline.scripted_proposer import ScriptedProposer

from ingestion.technique_source import TechniqueSource, TechniqueSourceResolver
from proposer.service import ProposerService
from validator.service import ValidatorService

SCENARIO = {
    "name": "plan-chain",
    "objective": "Escalate then loot, preferring undetected actions.",
    "operator": "eval@local.test",
    "target_scope": "projects/unit-sandbox",
    "environment": {
        "identity": "start@unit-sandbox.iam.gserviceaccount.com",
        "starting_permissions": ["a.read", "b.actAs"],
    },
    "techniques": [
        {
            "id": "t.recon",
            "tactic": "discovery",
            "service": "iam",
            "required_permissions": ["a.read"],
        },
        {"id": "t.escalate", "required_permissions": ["b.actAs"]},
        {"id": "t.loot", "required_permissions": ["c.get"]},
    ],
    "detections": [{"id": "d.noop", "permissions": ["z.unused"]}],
    "solution": [
        {
            "command": "t.recon",
            "description": "Enumerate.",
            "output": {"discovered_resources": ["r1"]},
        },
        {
            "command": "t.escalate",
            "description": "Escalate.",
            "output": {"gained_permissions": ["c.get"]},
        },
        {"command": "t.loot", "description": "Exfiltrate.", "output": {"note": "done"}},
    ],
    "evaluation": {"match_policy": "top_ranked"},
}


def _run(tmp_path, oracle):
    scenario = EvaluationScenario.model_validate(SCENARIO)
    knowledge = build_knowledge(scenario, tmp_path / "artifacts")
    proposer = ProposerService(
        snapshots=knowledge.snapshots, gemini=ScriptedProposer(oracle=oracle)
    )
    validator = ValidatorService(snapshots=knowledge.snapshots)
    outcome = Orchestrator(knowledge, proposer, validator).run()
    return scenario, knowledge, outcome


class _FakeResolver:
    """Return a canned runbook for one technique id, None otherwise."""

    def __init__(self, technique_id, markdown):
        self._technique_id = technique_id
        self._markdown = markdown

    def resolve(self, technique_id):
        if technique_id != self._technique_id:
            return None
        return TechniqueSource(technique_id, "hacktricks", Path("x.md"), self._markdown)


def test_plan_lists_validated_path_and_gains(tmp_path):
    scenario, knowledge, outcome = _run(tmp_path, oracle=("t.recon", "t.escalate", "t.loot"))
    plan = build_plan(scenario, knowledge, outcome, resolver=None)
    assert outcome.passed
    assert "# Attack plan — plan-chain" in plan
    assert "VALIDATED (3/3 steps validated)" in plan
    # Every validated technique appears in the ordered path and as an execution section.
    for technique_id in ("t.recon", "t.escalate", "t.loot"):
        assert f"`{technique_id}`" in plan
    assert "### Step 1: `t.recon`" in plan
    # Gains from the scripted step output are surfaced.
    assert "resources `r1`" in plan
    assert "permissions `c.get`" in plan
    # No corpus source available for inline techniques -> explicit fallback, not a crash.
    assert "_No source runbook available for this technique._" in plan


def test_plan_embeds_resolved_runbook(tmp_path):
    scenario, knowledge, outcome = _run(tmp_path, oracle=("t.recon", "t.escalate", "t.loot"))
    resolver = _FakeResolver("t.escalate", "### exploit\n\n```bash\ngcloud do-thing\n```")
    plan = build_plan(scenario, knowledge, outcome, resolver=resolver)
    assert "gcloud do-thing" in plan
    assert "Runbook from IAMouflage" in plan


def test_plan_reports_partial_when_run_deviates(tmp_path):
    # Oracle proposes escalate first, so step 0 deviates and the run stops.
    scenario, knowledge, outcome = _run(tmp_path, oracle=("t.escalate", "t.recon", "t.loot"))
    plan = build_plan(scenario, knowledge, outcome, resolver=None)
    assert not outcome.passed
    assert "No step was validated" in plan


# --- source resolver -----------------------------------------------------------------------

def _iamouflage_layout(tmp_path, records):
    data = tmp_path / "IAMouflage" / "code" / "data"
    data.mkdir(parents=True)
    (data / "techniques.json").write_text(json.dumps(records), encoding="utf-8")
    corpus = tmp_path / "IAMouflage" / "data" / "techniques"
    hacktricks = corpus / "hacktricks-cloud" / "src" / "pentesting-cloud" / "gcp-security"
    stratus = corpus / "stratus-red-team" / "v2" / "internal" / "attacktechniques" / "gcp"
    hacktricks.mkdir(parents=True)
    stratus.mkdir(parents=True)
    return data, hacktricks, stratus


def test_resolver_slices_hacktricks_section(tmp_path):
    data, hacktricks, _ = _iamouflage_layout(
        tmp_path,
        [
            {
                "id": "t.hack",
                "source": "hacktricks",
                "rel_path": "foo.md",
                "line": 3,
                "heading_level": 3,
            }
        ],
    )
    (hacktricks / "foo.md").write_text(
        "# GCP page\n"
        "## section\n"
        "### t.hack heading\n"
        "run this:\n"
        "```bash\ngcloud do-thing\n```\n"
        "#### sub note\n"
        "still part\n"
        "### other technique\n"
        "not included\n",
        encoding="utf-8",
    )
    resolved = TechniqueSourceResolver(data).resolve("t.hack")
    assert resolved is not None
    assert "gcloud do-thing" in resolved.markdown
    assert "still part" in resolved.markdown  # deeper #### heading does not end the section
    assert "other technique" not in resolved.markdown  # equal-level ### heading does
    assert "not included" not in resolved.markdown


def test_resolver_returns_whole_stratus_file(tmp_path):
    data, _, stratus = _iamouflage_layout(
        tmp_path,
        [
            {
                "id": "t.stratus",
                "source": "stratus",
                "extraction": "stratus",
                "rel_path": "x/main.go",
            }
        ],
    )
    (stratus / "x").mkdir(parents=True)
    (stratus / "x" / "main.go").write_text("package main\n// whole file\n", encoding="utf-8")
    resolved = TechniqueSourceResolver(data).resolve("t.stratus")
    assert resolved is not None
    assert resolved.source == "stratus"
    assert "whole file" in resolved.markdown


def test_resolver_missing_and_unknown_return_none(tmp_path):
    data, _, _ = _iamouflage_layout(
        tmp_path,
        [
            {
                "id": "t.gone",
                "source": "hacktricks",
                "rel_path": "absent.md",
                "line": 2,
                "heading_level": 3,
            }
        ],
    )
    resolver = TechniqueSourceResolver(data)
    assert resolver.resolve("t.gone") is None  # record exists but the md file does not
    assert resolver.resolve("t.unknown") is None  # no such record
