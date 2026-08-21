# Proposer / Validator evaluation pipeline

This pipeline exercises **only** the `proposer` and `validator` from `code/`. It has no
IAMouflage export, environment brain, green agent, or spawned infrastructure. Instead of
executing anything, each scenario carries a ground-truth `solution` (ordered commands plus a
scripted `output`), and the pipeline checks whether the model's ranked, validator-approved plan
follows that solution — advancing the environment by applying the scripted `output` when it
does, and stopping when it deviates. This lets you directly inspect and tweak the model output.

## Flow

![flow](../../docs/evaluation-pipeline.png)

| Diagram box | Module | Role |
| --- | --- | --- |
| Parser | `parser.py` + `scenario.py` + `knowledge.py` | Validate the scenario and build an immutable, permission-indexed `MatrixSnapshot` — either from the inline catalog (Mode A) or by ingesting the real IAMouflage manual (Mode B). |
| Orchestrator | `orchestrator.py` | Hold env state, drive the agentic loop, track all entities. |
| Prompt Builder | `prompt_builder.py` | Shape the `ProposalRequest` from the current knowledge. |
| Proposer | real `proposer.service.ProposerService` | Rank cataloged techniques (live Gemini, or a scripted stub offline). |
| Validator | real `validator.service.ValidatorService` | Gate each proposal on feasibility + detection coverage (bitset + Z3). |
| Evaluator | `evaluator.py` | Compare the *accepted* plans to the expected solution step. |

### Proposer ↔ validator is an agentic inner loop (not the evaluator)

For each solution step the orchestrator runs a bounded proposer↔validator loop: the model
proposes ranked techniques, and the validator judges each one. A proposal is **rejected** when it
(a) requires a permission the identity does not hold (hallucinated/infeasible) or (b) has a
footprint that touches a loaded detection. Rejection reasons are fed back into the next round's
prompt (`previous_rejections`) and the rejected technique is excluded, so the model **re-proposes**
— up to `maximum_candidates` accepted plans or `maximum_rounds` rounds. Only accepted plans leave
this loop. The **evaluator** is a separate, later stage: it checks those accepted plans against the
ground-truth `solution`. Validator = "is this proposition legal/undetected?"; evaluator = "is it the
planned move?".

## Two knowledge modes

The catalog and detection coverage come from one of two sources, chosen per scenario:

- **Mode A — inline.** The scenario lists its own `techniques` and `detections`. The proposer is
  handed the relevant candidates directly. Small, fully deterministic, and the CI/test path.
- **Mode B — IAMouflage.** The scenario omits `techniques`/`detections` and sets a
  `detection_provider` (e.g. `sigma`). The full IAMouflage manual (≈250 techniques, ≈10k
  permissions) is ingested and coverage is the provider-filtered corpus. A live run is **agentic**:
  the model retrieves what it needs from the manual with read-only tools (`search_techniques`,
  `get_technique`, `search_detections`, `list_services`) instead of receiving a slice. Only
  tool-returned technique IDs are usable, and any ID outside the catalog is hard-rejected.

The `detection_provider` filter is scenario-level and bounds **both** what the model can query and
what the validator checks against, so the loaded manual stays a controlled variable.

## Run

From `code/` (or anywhere):

```bash
# live model (needs GEMINI_API_KEY in code/.env)
python evaluation-pipeline/run.py --scenario=evaluation-pipeline/scenario.yaml

# offline plumbing check (scripted oracle proposer, no key, no network)
python evaluation-pipeline/run.py --scenario=evaluation-pipeline/scenario.yaml --offline

# IAMouflage mode: catalog + sigma coverage from the real manual (agentic when live)
python evaluation-pipeline/run.py --scenario=evaluation-pipeline/scenarios/iamouflage-sigma-cloudbuild-actas.yaml

# batch: run every scenario in scenarios/ into one timestamped batch folder
python evaluation-pipeline/run.py --all --offline
```

Equivalent module form (from `code/evaluation-pipeline/`, with `code/` on `PYTHONPATH`):
`python -m evaluation_pipeline --scenario=scenario.yaml`.

A single run writes an isolated working set under `evaluation-pipeline/runs/<timestamp>_<name>/`
(a `--all` batch writes `runs/<timestamp>_batch_<id>/<scenario-name>/` per scenario plus a
`batch-summary.json`):

- `report.json` / `report.md` — PASS/FAIL, per-step expected-vs-published plans, and every
  candidate's validator verdict (feasible / outside-coverage / matching detections);
- `run-state.json` — current phase and terminal status;
- `model-conversation.jsonl` — redacted system/user/assistant conversation, thought summaries,
  usage, retries;
- `runtime/trace.jsonl` and `progress.log` — orchestrator/validator/model diagnostics.

The exit code is `0` on PASS (all solution steps followed) and `1` on FAIL.

## Scenario contract

A scenario is self-contained (see `scenario.example.yaml` for a commented Mode A template,
`scenario.yaml` for a real inline one, and `scenarios/iamouflage-sigma-cloudbuild-actas.yaml` for
Mode B):

- `environment` — the controlled identity, its `roles` (informational) and `starting_permissions`
  (what the validator reasons over).
- `techniques` — *(Mode A)* the catalog the proposer may pick from: `id`, `required_permissions`,
  optional `footprint_permissions` (defaults to required), `grants`, and metadata. Omit for Mode B.
- `detections` — *(Mode A)* the loaded coverage. Each rule's `permissions` define what the
  validator treats as monitored; a technique whose footprint intersects any is rejected as
  detected. Omit for Mode B.
- `detection_provider` — *(Mode B only)* the detection corpus to ingest from IAMouflage (e.g.
  `sigma`). Required when `techniques` is omitted; forbidden alongside an inline catalog. The
  `solution` commands must be real IAMouflage technique IDs (checked at build time).
- `solution` — the ground truth: an ordered list of `command` (a technique id) plus the scripted
  `output` (`gained_permissions`, `discovered_resources`, `gained_capabilities`) applied to the
  env when the model follows that step.
- `model` — pins the Gemini model, fallbacks, API mode, thinking level, deadline, and attempts.
- `evaluation` — `match_policy` (`top_ranked` | `within_candidates`), `maximum_candidates`,
  `maximum_proposals` (per round), `maximum_rounds` (proposer↔validator re-proposal rounds per
  step), `maximum_steps`, `stop_on_deviation`, and `require_admissible` (set `false` to publish
  the top-ranked plans regardless of the validator and skip re-proposal, isolating the proposer's
  ranking from the validator's gate).

## Scoring

A step is **followed** when the expected command is the top-ranked admissible plan
(`top_ranked`) or appears among the published plans (`within_candidates`). The run **passes** when
every solution step is followed. Because the validator only ever publishes admissible plans, a
pass simultaneously demonstrates that the proposer chose the intended technique **and** that the
validator judged it feasible and outside the loaded detection coverage.

## Tests

```bash
python -m pytest evaluation-pipeline/tests -q
```

The tests inject the scripted proposer, so they need no API key or network.
