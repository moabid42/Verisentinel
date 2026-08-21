from __future__ import annotations

from threading import RLock

from core.errors import AuthorizationError, NotFoundError
from core.security import stable_digest


class InMemoryCredentialVault:
    """Keep bearer credentials inside the execution boundary without persisting them."""

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._lock = RLock()

    def register_access_token(self, identity: str, access_token: str) -> str:
        if not access_token.strip():
            raise AuthorizationError("the starting access token is empty")
        reference = f"access-token:{stable_digest({'identity': identity, 'token': access_token})}"
        with self._lock:
            self._credentials[reference] = access_token
        return reference

    def resolve(self, reference: str) -> str:
        with self._lock:
            try:
                return self._credentials[reference]
            except KeyError as error:
                raise NotFoundError(f"credential reference {reference!r} is not loaded") from error
