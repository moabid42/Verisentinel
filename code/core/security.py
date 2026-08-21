from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from core.errors import AuthorizationError


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def verify_control_token(expected: str | None, supplied: str | None) -> None:
    if not expected:
        raise AuthorizationError("control-plane token is not configured")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise AuthorizationError("invalid control-plane token")
