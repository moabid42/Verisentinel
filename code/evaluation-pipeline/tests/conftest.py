"""Make the sibling packages importable for the test suite.

The pipeline lives in ``code/evaluation-pipeline`` and its dependencies (core, proposer,
validator, ingestion) live one level up in ``code``. Putting both on ``sys.path`` here — once,
before any test module is imported — lets the tests use ordinary top-level imports instead of a
per-file path hack.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1]  # code/evaluation-pipeline
_CODE = _PIPELINE.parent  # code
for _entry in (str(_CODE), str(_PIPELINE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
