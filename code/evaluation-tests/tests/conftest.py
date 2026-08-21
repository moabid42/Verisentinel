from __future__ import annotations

import sys
from pathlib import Path

EVALUATION_TESTS = Path(__file__).resolve().parents[1]
CODE_ROOT = EVALUATION_TESTS.parent
sys.path.insert(0, str(EVALUATION_TESTS))
sys.path.insert(0, str(CODE_ROOT))
