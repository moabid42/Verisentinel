class PlannerError(Exception):
    """Base error for expected planner failures."""


class DataConsistencyError(PlannerError):
    """An input source cannot be represented without losing semantics."""


class NotFoundError(PlannerError):
    """A requested immutable record does not exist."""


class VersionConflictError(PlannerError):
    """Two records use incompatible state or matrix versions."""


class AuthorizationError(PlannerError):
    """An operation crossed an approval or target authorization boundary."""


_HTTP_STATUS: dict[type[PlannerError], int] = {
    NotFoundError: 404,
    VersionConflictError: 409,
    AuthorizationError: 403,
    DataConsistencyError: 422,
}


def http_status_for(error: PlannerError) -> int:
    """HTTP status an API layer should return for an expected planner error."""
    for error_type, status in _HTTP_STATUS.items():
        if isinstance(error, error_type):
            return status
    return 400

