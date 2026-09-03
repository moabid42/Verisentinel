"""Durable, non-sensitive records for interactive terminal sessions."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from core.ids import new_id
from core.models import ImmutableModel, utc_now
from core.persistence import JsonModelStore

_SESSION_ID_PATTERN = re.compile(r"^session_[0-9a-f]{32}$")
_MAX_RECORDED_EVENTS = 100


class ShellSessionStatus(StrEnum):
    """Lifecycle state for an interactive terminal session."""

    ACTIVE = "active"
    CLOSED = "closed"
    INTERRUPTED = "interrupted"


class ShellEvent(ImmutableModel):
    """One sanitized command outcome retained in a shell session."""

    sequence: int = Field(ge=1)
    operation: str = Field(min_length=1, max_length=64)
    exit_code: int = Field(ge=0, le=255)
    recorded_at: datetime = Field(default_factory=utc_now)


class ShellSession(ImmutableModel):
    """Bounded persisted state for one terminal workspace."""

    session_id: str = Field(pattern=_SESSION_ID_PATTERN.pattern)
    status: ShellSessionStatus = ShellSessionStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    command_count: int = Field(default=0, ge=0)
    events: tuple[ShellEvent, ...] = Field(default=(), max_length=_MAX_RECORDED_EVENTS)


class ShellSessionRepository:
    """Persist terminal session metadata through atomic JSON replacement."""

    def __init__(self, directory: Path) -> None:
        self.store = JsonModelStore(directory, ShellSession)

    def create(self) -> ShellSession:
        """Create and persist a new active session."""
        session = ShellSession(session_id=new_id("session"))
        self.store.put(session.session_id, session)
        return session

    def get(self, session_id: str) -> ShellSession:
        """Load one session after validating its opaque identifier."""
        _validate_session_id(session_id)
        return self.store.get(session_id)

    def list(self) -> tuple[ShellSession, ...]:
        """Return sessions from most to least recently updated."""
        sessions = tuple(self.store.get(key) for key in self.store.list_keys())
        return tuple(
            sorted(
                sessions,
                key=lambda session: (session.updated_at, session.session_id),
                reverse=True,
            )
        )

    def resume(self, session_id: str) -> ShellSession:
        """Mark an existing session active and return it."""
        session = self.get(session_id).model_copy(
            update={"status": ShellSessionStatus.ACTIVE, "updated_at": utc_now()}
        )
        self.store.put(session.session_id, session)
        return session

    def record(
        self,
        session: ShellSession,
        *,
        operation: str,
        exit_code: int,
    ) -> ShellSession:
        """Append one sanitized command outcome to a session."""
        event = ShellEvent(
            sequence=session.command_count + 1,
            operation=operation,
            exit_code=exit_code,
        )
        events = (*session.events, event)[-_MAX_RECORDED_EVENTS:]
        updated = session.model_copy(
            update={
                "status": ShellSessionStatus.ACTIVE,
                "updated_at": event.recorded_at,
                "command_count": event.sequence,
                "events": events,
            }
        )
        self.store.put(updated.session_id, updated)
        return updated

    def close(
        self,
        session: ShellSession,
        *,
        interrupted: bool = False,
    ) -> ShellSession:
        """Persist a terminal session's final lifecycle state."""
        status = (
            ShellSessionStatus.INTERRUPTED
            if interrupted
            else ShellSessionStatus.CLOSED
        )
        updated = session.model_copy(update={"status": status, "updated_at": utc_now()})
        self.store.put(updated.session_id, updated)
        return updated


def _validate_session_id(session_id: str) -> None:
    if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError("session ID has an invalid format")
