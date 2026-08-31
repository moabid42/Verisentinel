from __future__ import annotations

from core.errors import AuthorizationError
from core.models import ApprovalRecord, ExecutionRequest
from core.security import stable_digest
from execution.models import EngagementAuthorization


def target_is_allowed(target: str, allowed_scope: str) -> bool:
    normalized_scope = allowed_scope.rstrip("/")
    return target == normalized_scope or target.startswith(f"{normalized_scope}/")


def verify_execution_authority(
    request: ExecutionRequest,
    approval: ApprovalRecord,
    authorization: EngagementAuthorization,
) -> None:
    if not authorization.enabled:
        raise AuthorizationError("execution is disabled for this engagement")
    if request.engagement_id != authorization.engagement_id:
        raise AuthorizationError("execution request does not match the authorized engagement")
    if request.state_version != authorization.state_version:
        raise AuthorizationError("execution request uses a stale environment state")
    if request.matrix_version != authorization.matrix_version:
        raise AuthorizationError("execution request uses a stale matrix snapshot")
    if not any(
        target_is_allowed(request.target, scope) for scope in authorization.allowed_targets
    ):
        raise AuthorizationError(
            f"target {request.target!r} is outside the engagement target allow-list"
        )

    bound_fields = (
        "engagement_id",
        "candidate_id",
        "action_id",
        "identity",
        "target",
        "validator_result_id",
        "state_version",
        "matrix_version",
        "credential_ref",
        "artifact_digest",
        "artifact_path",
    )
    changed = [
        field
        for field in bound_fields
        if getattr(request, field) != getattr(approval, field)
    ]
    if request.approval_id != approval.approval_id:
        changed.append("approval_id")
    if stable_digest(request.arguments.model_dump(mode="json")) != approval.arguments_digest:
        changed.append("arguments")
    if changed:
        raise AuthorizationError(
            "execution request differs from its approval: " + ", ".join(changed)
        )
