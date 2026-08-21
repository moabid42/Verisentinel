# Thesis evidence tests

This directory contains the reproducible evidence harness for Chapter 5. It is separate from
`evaluation-pipeline/`: that directory runs proposer/validator scenarios, while this directory
checks the thesis claims against frozen corpus and run artifacts and produces JSON, CSV, Markdown,
and SVG evidence.

## Evidence streams

1. **Validator verification (RQ1).** Compare the direct bitset and Z3 implementations over every
   state, coverage, requirement, and footprint vector at widths 1--4, add seeded boundary-width
   cases, and measure both implementations' latency.
2. **Normalization and projection audit (RQ2).** Check every normalized detection operation and
   technique record for traceability and canonical output, recompute directly checkable mappings
   from pinned references, reconstruct all flattened detection rows, inventory projection loss,
   and rebuild the complete planner matrix twice.
3. **Manual scenario-plan review (RQ3).** Validate one frozen successful model report for every
   bundled offline scenario, require each reviewer-selected step to have been published and
   validator-admissible, preserve the human judgments and limitations in a manifest, and render one
   stable reviewed plan per scenario.

The older plan-quality calibration fixtures remain as an auxiliary demonstration that Boolean hard
checks do not decide semantic quality. They are not used as the RQ3 result.

## Layout

- `cases/manual-plan-reviews.json` -- reviewer judgments, rationales, report paths, and limitations.
- `evaluation_tests/normalization_audit.py` -- exhaustive RQ2 audit and matrix rebuild.
- `evaluation_tests/manual_plan_review.py` -- automatic RQ3 evidence checks and plan renderer.
- `tests/` -- offline regression tests; no model key or network is required.
- `results/evidence-2026-08-16/` -- generated evidence reported in the thesis.
- `cases/plan-quality-cases.json`, `rubrics/`, and `prompts/` -- auxiliary quality-gate
  calibration material.

## Run

From `code/`:

```bash
.venv/bin/python -m pytest evaluation-tests/tests -q
.venv/bin/python evaluation-tests/run.py --full
```

`--full` performs the exhaustive widths 1--4 comparison, 10,000 seeded boundary cases, the
corpus-wide RQ2 audit, and the frozen RQ3 review. `--quick` is intended only for development. To
regenerate the non-timing evidence while preserving an existing full equivalence and latency
measurement, use:

```bash
.venv/bin/python evaluation-tests/run.py --full --reuse-measurements
```

The full runner writes `summary.json`, the corpus and normalization audits, manual-review JSON and
CSV, reviewed Markdown plans, equivalence and latency CSV files, and SVG figures. It also copies
the paper figures into `paper/imgs/evaluation/`.

## Interpretation boundary

- RQ2's passing result means traceable, pinned-reference-consistent, and deterministically
  rebuildable normalization for the frozen inputs. It is not an independent semantic accuracy
  score for the upstream rules or reference tables.
- RQ3 is a single-reviewer, non-blinded sufficiency review of one already-successful report per
  scenario. It does not estimate model reliability.
- Scripted state changes replace cloud execution, and no independent defender oracle is present.
  `outside_loaded_coverage` must not be interpreted as real-world non-detection.
