"""Standalone entry point for the proposer/validator evaluation pipeline.

Puts both `code/` (for core/proposer/validator) and this directory (for the
`evaluation_pipeline` package) on sys.path, then dispatches to the CLI. Run from anywhere:

    python evaluation-pipeline/run.py --scenario=evaluation-pipeline/scenario.yaml
    python evaluation-pipeline/run.py --scenario=evaluation-pipeline/scenario.yaml --offline
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # code/evaluation-pipeline
CODE_DIRECTORY = HERE.parent  # code
for entry in (str(CODE_DIRECTORY), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from evaluation_pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
