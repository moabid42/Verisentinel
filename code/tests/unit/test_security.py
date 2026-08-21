import pytest

from core.errors import AuthorizationError
from core.security import verify_control_token


def test_control_plane_authentication_fails_closed_without_configuration() -> None:
    with pytest.raises(AuthorizationError, match="not configured"):
        verify_control_token(None, None)


def test_control_plane_authentication_rejects_wrong_token() -> None:
    with pytest.raises(AuthorizationError, match="invalid"):
        verify_control_token("expected", "wrong")


def test_control_plane_authentication_accepts_exact_token() -> None:
    verify_control_token("expected", "expected")
