"""Execution provider protocol."""

from __future__ import annotations

from typing import Literal, Protocol

from core.models import ExecutionObservation, ExecutionSpec
from execution.credentials import CredentialLease

ExecutionProviderName = Literal["simulator", "capsule", "evaluation", "gcp"]


class ExecutionProvider(Protocol):
    """Execute one immutable approved specification."""

    name: ExecutionProviderName

    def execute(
        self,
        spec: ExecutionSpec,
        credential_lease: CredentialLease,
    ) -> ExecutionObservation: ...
