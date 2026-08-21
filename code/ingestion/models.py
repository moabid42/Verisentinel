from __future__ import annotations

from pydantic import Field

from core.models import ImmutableModel


class BuildSnapshotRequest(ImmutableModel):
    enabled_detection_ids: tuple[str, ...] = ()
    enabled_sources: tuple[str, ...] = ()
    strict: bool = True


class PermissionResolution(ImmutableModel):
    aliases: dict[str, str] = Field(default_factory=dict)
    extensions: dict[str, str] = Field(default_factory=dict)

