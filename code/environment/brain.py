from __future__ import annotations

from core.config import Paths
from core.errors import DataConsistencyError, NotFoundError, VersionConflictError
from core.models import EnvironmentSnapshot, MatrixSnapshot, StateVector
from core.security import stable_digest
from environment.models import ApplyObservationRequest, InitializeEnvironmentRequest
from environment.repository import EnvironmentRepository
from ingestion.snapshot import SnapshotRepository


def context_key(identity: str, scope: str) -> str:
    return f"{identity}\x1f{scope}"


class EnvironmentBrain:
    def __init__(
        self,
        repository: EnvironmentRepository | None = None,
        snapshots: SnapshotRepository | None = None,
        paths: Paths | None = None,
    ) -> None:
        paths = paths or Paths()
        self.repository = repository or EnvironmentRepository(paths.runtime / "environment")
        self.snapshots = snapshots or SnapshotRepository(paths.artifacts / "snapshots")

    def initialize(self, request: InitializeEnvironmentRequest) -> EnvironmentSnapshot:
        try:
            self.repository.current(request.engagement_id)
        except NotFoundError:
            pass
        else:
            raise DataConsistencyError(
                f"engagement {request.engagement_id!r} already has an environment"
            )

        matrix = self.snapshots.get(request.matrix_version)
        indices = self._permission_indices(request.permissions, matrix)
        state_version = self._state_version(
            request.engagement_id,
            request.matrix_version,
            request.objective,
            {
                context_key(request.identity, request.scope): {
                    "identity": request.identity,
                    "credential": request.credential,
                    "scope": request.scope,
                    "permission_indices": indices,
                    "source": request.source,
                }
            },
            (),
            (),
            (),
        )
        vector = StateVector(
            state_version=state_version,
            engagement_id=request.engagement_id,
            matrix_version=request.matrix_version,
            identity=request.identity,
            credential=request.credential,
            scope=request.scope,
            permission_indices=indices,
            source=request.source,
        )
        return self.repository.publish(
            EnvironmentSnapshot(
                engagement_id=request.engagement_id,
                state_version=state_version,
                matrix_version=request.matrix_version,
                objective=request.objective,
                identities={context_key(request.identity, request.scope): vector},
            )
        )

    def current(self, engagement_id: str) -> EnvironmentSnapshot:
        return self.repository.current(engagement_id)

    def state_vector(
        self, engagement_id: str, identity: str, scope: str | None = None
    ) -> StateVector:
        environment = self.current(engagement_id)
        matches = tuple(
            vector
            for vector in environment.identities.values()
            if vector.identity == identity and (scope is None or vector.scope == scope)
        )
        if not matches:
            raise NotFoundError(
                f"no state vector for identity {identity!r} and scope {scope!r}"
            )
        if len(matches) > 1:
            raise DataConsistencyError(
                f"identity {identity!r} has multiple scopes; an exact scope is required"
            )
        return matches[0]

    def apply_observation(self, request: ApplyObservationRequest) -> EnvironmentSnapshot:
        observation = request.observation
        current = self.current(observation.engagement_id)
        if current.state_version != request.expected_state_version:
            raise VersionConflictError(
                f"environment moved from {request.expected_state_version} to "
                f"{current.state_version}"
            )
        matrix = self.snapshots.get(current.matrix_version)
        matches = [
            (key, vector)
            for key, vector in current.identities.items()
            if vector.identity == observation.identity
        ]
        if len(matches) != 1:
            raise DataConsistencyError(
                f"observation identity {observation.identity!r} resolves to {len(matches)} contexts"
            )

        key, old_vector = matches[0]
        known_permissions = set(matrix.permissions)
        unknown = (
            set(observation.gained_permissions)
            | set(observation.revoked_permissions)
            | set(observation.observed_permission_footprint)
        ) - known_permissions
        if unknown:
            raise DataConsistencyError(
                f"observation references permissions outside matrix {matrix.matrix_version}: "
                f"{', '.join(sorted(unknown))}"
            )
        index = self._column_index(matrix)
        permission_indices = set(old_vector.permission_indices)
        permission_indices.update(index[p] for p in observation.gained_permissions)
        permission_indices.difference_update(index[p] for p in observation.revoked_permissions)

        discovered = tuple(
            sorted(set(current.discovered_resources) | set(observation.discovered_resources))
        )
        capabilities = tuple(
            sorted(set(current.capabilities) | set(observation.gained_capabilities))
        )
        completed = tuple(
            [*current.completed_actions, observation.action_id]
            if observation.success
            else current.completed_actions
        )
        vector_payloads = {
            item_key: {
                "identity": vector.identity,
                "credential": vector.credential,
                "scope": vector.scope,
                "permission_indices": (
                    tuple(sorted(permission_indices))
                    if item_key == key
                    else vector.permission_indices
                ),
                "source": (
                    f"execution:{observation.execution_id}" if item_key == key else vector.source
                ),
            }
            for item_key, vector in current.identities.items()
        }
        state_version = self._state_version(
            current.engagement_id,
            current.matrix_version,
            current.objective,
            vector_payloads,
            discovered,
            capabilities,
            completed,
        )
        identities = {
            item_key: StateVector(
                state_version=state_version,
                engagement_id=current.engagement_id,
                matrix_version=current.matrix_version,
                identity=payload["identity"],
                credential=payload["credential"],
                scope=payload["scope"],
                permission_indices=payload["permission_indices"],
                source=payload["source"],
            )
            for item_key, payload in vector_payloads.items()
        }
        return self.repository.publish(
            EnvironmentSnapshot(
                engagement_id=current.engagement_id,
                state_version=state_version,
                matrix_version=current.matrix_version,
                objective=current.objective,
                identities=identities,
                discovered_resources=discovered,
                capabilities=capabilities,
                completed_actions=completed,
            )
        )

    @staticmethod
    def render_summary(environment: EnvironmentSnapshot) -> dict:
        return {
            "engagement_id": environment.engagement_id,
            "state_version": environment.state_version,
            "objective": environment.objective,
            "identities": [
                {
                    "identity": vector.identity,
                    "scope": vector.scope,
                    "available_permission_count": len(vector.permission_indices),
                }
                for vector in environment.identities.values()
            ],
            "discovered_resources": list(environment.discovered_resources),
            "capabilities": list(environment.capabilities),
            "completed_actions": list(environment.completed_actions),
        }

    @staticmethod
    def _column_index(matrix: MatrixSnapshot) -> dict[str, int]:
        return {permission: position for position, permission in enumerate(matrix.permissions)}

    @staticmethod
    def _permission_indices(
        permissions: tuple[str, ...], matrix: MatrixSnapshot
    ) -> tuple[int, ...]:
        index = EnvironmentBrain._column_index(matrix)
        unknown = set(permissions) - set(index)
        if unknown:
            raise DataConsistencyError(
                f"state contains permissions outside matrix {matrix.matrix_version}: "
                f"{', '.join(sorted(unknown))}"
            )
        return tuple(sorted({index[permission] for permission in permissions}))

    @staticmethod
    def _state_version(
        engagement_id: str,
        matrix_version: str,
        objective: str,
        identities: dict,
        resources: tuple[str, ...],
        capabilities: tuple[str, ...],
        completed_actions: tuple[str, ...],
    ) -> str:
        return stable_digest(
            {
                "engagement_id": engagement_id,
                "matrix_version": matrix_version,
                "objective": objective,
                "identities": identities,
                "resources": resources,
                "capabilities": capabilities,
                "completed_actions": completed_actions,
            }
        )
