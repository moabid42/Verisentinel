from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import DataConsistencyError
from core.security import stable_digest


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise DataConsistencyError(f"required IAM dataset file is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class IAMDataset:
    permissions: tuple[str, ...]
    version: str

    @classmethod
    def load(cls, directory: Path) -> IAMDataset:
        raw_permissions = _load_json(directory / "permissions.json")
        if not isinstance(raw_permissions, dict):
            raise DataConsistencyError("permissions.json must be a permission-keyed object")
        # Only `permissions` is consumed downstream, but the version digest still covers the
        # whole dataset so any upstream change to roles/methods/tags/etc. yields a fresh
        # matrix_version. Keep the payload keys and order stable to preserve existing digests.
        structural_payload = {
            "permissions": sorted(raw_permissions),
            "permission_roles": _load_json(directory / "role_permissions.json"),
            "predefined_roles": _load_json(directory / "predefined_roles.json"),
            "method_permissions": _load_json(directory / "map.json"),
            "methods": _load_json(directory / "methods.json"),
            "methods_ext": _load_json(directory / "methods_ext.json"),
            "service_mapping": _load_json(directory / "service_mapping.json"),
            "tags": _load_json(directory / "tags.json"),
        }
        return cls(
            permissions=tuple(sorted(raw_permissions)),
            version=stable_digest(structural_payload),
        )
