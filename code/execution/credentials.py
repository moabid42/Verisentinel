"""Credential source parsing and short-lived lease resolution."""

from __future__ import annotations

import getpass
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Protocol

import google.auth
import httpx
from google.auth import impersonated_credentials
from google.auth.credentials import Credentials as GoogleCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials as ServiceAccountKeyCredentials

from core.errors import AuthorizationError, DataConsistencyError
from core.models import CREDENTIAL_REFERENCE_PATTERN

_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$"
)
_GOOGLE_CLOUD_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
)
_TOKEN_INFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class CredentialSourceError(ValueError):
    """A credential source descriptor is invalid."""


class CredentialSourceKind(StrEnum):
    """Supported credential source kinds."""

    STDIN = "stdin"
    FILE = "file"
    ENV = "env"
    ADC = "adc"
    IMPERSONATE = "impersonate"


@dataclass(frozen=True, slots=True, repr=False)
class CredentialSource:
    """A non-secret descriptor for locating credential material."""

    kind: CredentialSourceKind
    locator: str | None = None

    def __repr__(self) -> str:
        locator = "[CONFIGURED]" if self.locator is not None else None
        return f"CredentialSource(kind={self.kind!r}, locator={locator!r})"


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Verified non-secret metadata for a supplied access token."""

    principal: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CredentialInspection:
    """Verified, non-sensitive metadata for one credential source."""

    source_kind: CredentialSourceKind
    principal: str
    expires_at: datetime


class AccessTokenInspector(Protocol):
    """Verify the principal and expiry of a supplied access token."""

    def inspect(self, access_token: str) -> TokenMetadata: ...


@dataclass(slots=True, repr=False)
class CredentialLease:
    """One runtime-only lease over short-lived credential material."""

    principal: str
    expires_at: datetime
    source_kind: CredentialSourceKind
    _access_token: str = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.principal.strip():
            raise AuthorizationError("credential lease has no verified principal")
        if not self._access_token.strip():
            raise AuthorizationError("credential lease contains no access token")
        self.expires_at = _as_utc(self.expires_at)

    @property
    def access_token(self) -> str:
        """Return material only while the lease is active."""
        if self._closed:
            raise AuthorizationError("credential lease is closed")
        return self._access_token

    @property
    def closed(self) -> bool:
        """Whether the lease material has been cleared."""
        return self._closed

    def close(self) -> None:
        """Clear the lease's reference to credential material."""
        self._access_token = ""
        self._closed = True

    def __enter__(self) -> CredentialLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "CredentialLease("
            f"principal={self.principal!r}, "
            f"expires_at={self.expires_at!r}, "
            f"source_kind={self.source_kind!r}, "
            "access_token='[REDACTED]', "
            f"closed={self.closed!r})"
        )


class GoogleAccessTokenInspector:
    """Inspect Google access tokens without placing them in request URLs."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def inspect(self, access_token: str) -> TokenMetadata:
        try:
            if self._client is None:
                response = httpx.post(
                    _TOKEN_INFO_URL,
                    data={"access_token": access_token},
                    timeout=10.0,
                )
            else:
                response = self._client.post(
                    _TOKEN_INFO_URL,
                    data={"access_token": access_token},
                )
            response.raise_for_status()
            payload = response.json()
            principal = str(payload["email"]).strip()
            expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise AuthorizationError(
                "supplied access token metadata could not be verified"
            ) from None
        if not principal:
            raise AuthorizationError(
                "supplied access token metadata has no verified principal"
            )
        return TokenMetadata(principal=principal, expires_at=expires_at)


class CredentialResolver:
    """Resolve registered references into verified, short-lived leases."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        stdin_reader: Callable[[], str] | None = None,
        token_inspector: AccessTokenInspector | None = None,
        adc_loader: Callable[..., tuple[GoogleCredentials, str | None]] | None = None,
        impersonated_factory: Callable[..., GoogleCredentials] | None = None,
        request_factory: Callable[[], object] | None = None,
        now: Callable[[], datetime] | None = None,
        minimum_ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        self._sources: dict[str, CredentialSource] = {}
        self._lock = RLock()
        self._environ = environ if environ is not None else os.environ
        self._stdin_reader = stdin_reader or _read_stdin
        self._token_inspector = token_inspector or GoogleAccessTokenInspector()
        self._adc_loader = adc_loader or google.auth.default
        self._impersonated_factory = (
            impersonated_factory or impersonated_credentials.Credentials
        )
        self._request_factory = request_factory or GoogleAuthRequest
        self._now = now or (lambda: datetime.now(UTC))
        self._minimum_ttl = minimum_ttl

    def register(
        self,
        credential_ref: str,
        source: CredentialSource,
    ) -> CredentialSource:
        """Associate one opaque reference with one non-secret source descriptor."""
        if re.fullmatch(CREDENTIAL_REFERENCE_PATTERN, credential_ref) is None:
            raise CredentialSourceError("credential reference has an invalid format")
        with self._lock:
            existing = self._sources.get(credential_ref)
            if existing is not None and existing != source:
                raise DataConsistencyError(
                    f"credential reference {credential_ref!r} cannot be rebound"
                )
            self._sources[credential_ref] = source
        return source

    def resolve(
        self,
        credential_ref: str,
        expected_principal: str,
    ) -> CredentialLease:
        """Resolve and verify one lease for an expected principal."""
        with self._lock:
            source = self._sources.get(credential_ref)
        if source is None:
            raise AuthorizationError(
                f"credential reference {credential_ref!r} has no configured source"
            )

        if source.kind == CredentialSourceKind.ADC:
            lease = self._resolve_adc(source)
        elif source.kind == CredentialSourceKind.IMPERSONATE:
            lease = self._resolve_impersonated(source, expected_principal)
        else:
            lease = self._resolve_supplied(source)
        try:
            self._validate_lease(lease, expected_principal)
        except Exception:
            lease.close()
            raise
        return lease

    def inspect(self, source: CredentialSource) -> CredentialInspection:
        """Resolve a source briefly and return only verified public metadata."""
        if source.kind == CredentialSourceKind.ADC:
            lease = self._resolve_adc(source)
        elif source.kind == CredentialSourceKind.IMPERSONATE:
            lease = self._resolve_impersonated(source, source.locator or "")
        else:
            lease = self._resolve_supplied(source)
        try:
            self._validate_expiry(lease)
            return CredentialInspection(
                source_kind=lease.source_kind,
                principal=lease.principal,
                expires_at=lease.expires_at,
            )
        finally:
            lease.close()

    def _resolve_supplied(self, source: CredentialSource) -> CredentialLease:
        access_token = self._read_supplied_token(source)
        try:
            metadata = self._token_inspector.inspect(access_token)
        except Exception as error:
            if isinstance(error, AuthorizationError):
                raise
            raise AuthorizationError(
                "supplied access token metadata could not be verified"
            ) from None
        return CredentialLease(
            principal=metadata.principal,
            expires_at=metadata.expires_at,
            source_kind=source.kind,
            _access_token=access_token,
        )

    def _resolve_adc(self, source: CredentialSource) -> CredentialLease:
        try:
            credentials, _ = self._adc_loader(scopes=_GOOGLE_CLOUD_SCOPES)
            _reject_service_account_key(credentials)
            credentials.refresh(self._request_factory())
            access_token = _credential_token(credentials)
            principal = _credential_principal(credentials)
            expires_at = _credential_expiry(credentials)
            if principal is None or expires_at is None:
                metadata = self._token_inspector.inspect(access_token)
                principal = principal or metadata.principal
                expires_at = expires_at or metadata.expires_at
        except Exception as error:
            if isinstance(error, AuthorizationError):
                raise
            raise AuthorizationError(
                "application default credentials could not be resolved"
            ) from None
        return CredentialLease(
            principal=principal,
            expires_at=expires_at,
            source_kind=source.kind,
            _access_token=access_token,
        )

    def _resolve_impersonated(
        self,
        source: CredentialSource,
        expected_principal: str,
    ) -> CredentialLease:
        target_principal = source.locator or ""
        if target_principal != expected_principal:
            raise AuthorizationError(
                "impersonation target does not match the expected principal"
            )
        try:
            source_credentials, _ = self._adc_loader(scopes=_GOOGLE_CLOUD_SCOPES)
            _reject_service_account_key(source_credentials)
            credentials = self._impersonated_factory(
                source_credentials=source_credentials,
                target_principal=target_principal,
                target_scopes=_GOOGLE_CLOUD_SCOPES,
                lifetime=3600,
            )
            credentials.refresh(self._request_factory())
            access_token = _credential_token(credentials)
            expires_at = _credential_expiry(credentials)
        except Exception as error:
            if isinstance(error, AuthorizationError):
                raise
            raise AuthorizationError(
                "impersonated credentials could not be resolved"
            ) from None
        if expires_at is None:
            raise AuthorizationError("impersonated credential expiry is unavailable")
        return CredentialLease(
            principal=target_principal,
            expires_at=expires_at,
            source_kind=source.kind,
            _access_token=access_token,
        )

    def _read_supplied_token(self, source: CredentialSource) -> str:
        if source.kind == CredentialSourceKind.STDIN:
            access_token = self._stdin_reader()
        elif source.kind == CredentialSourceKind.ENV:
            access_token = self._environ.get(source.locator or "", "")
        elif source.kind == CredentialSourceKind.FILE:
            access_token = _read_protected_file(Path(source.locator or ""))
        else:
            raise AuthorizationError("credential source is not a supplied-token source")
        access_token = access_token.strip()
        if not access_token:
            raise AuthorizationError("credential source contains no access token")
        return access_token

    def _validate_lease(
        self,
        lease: CredentialLease,
        expected_principal: str,
    ) -> None:
        if lease.principal != expected_principal:
            raise AuthorizationError(
                "credential principal does not match the expected principal"
            )
        self._validate_expiry(lease)

    def _validate_expiry(self, lease: CredentialLease) -> None:
        if lease.expires_at <= _as_utc(self._now()) + self._minimum_ttl:
            raise AuthorizationError("credential lease is expired or too close to expiry")


def parse_credential_source(value: str) -> CredentialSource:
    """Parse one supported non-secret credential source descriptor."""
    if value == CredentialSourceKind.STDIN:
        return CredentialSource(kind=CredentialSourceKind.STDIN)
    if value == CredentialSourceKind.ADC:
        return CredentialSource(kind=CredentialSourceKind.ADC)
    if ":" not in value:
        raise CredentialSourceError("unsupported credential source")

    raw_kind, locator = value.split(":", maxsplit=1)
    try:
        kind = CredentialSourceKind(raw_kind)
    except ValueError:
        raise CredentialSourceError("unsupported credential source") from None
    if not locator:
        raise CredentialSourceError("credential source locator cannot be empty")
    if kind == CredentialSourceKind.ENV:
        if _ENVIRONMENT_NAME_PATTERN.fullmatch(locator) is None:
            raise CredentialSourceError("credential environment name has an invalid format")
    elif kind == CredentialSourceKind.IMPERSONATE:
        if _SERVICE_ACCOUNT_PATTERN.fullmatch(locator) is None:
            raise CredentialSourceError(
                "impersonation principal must be a service account email"
            )
    elif kind != CredentialSourceKind.FILE:
        raise CredentialSourceError("credential source does not accept a locator")
    return CredentialSource(kind=kind, locator=locator)


def _read_stdin() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Access token: ")
    return sys.stdin.readline()


def _read_protected_file(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise AuthorizationError(
                "credential file permissions allow group or other access"
            )
        return path.read_text(encoding="utf-8")
    except AuthorizationError:
        raise
    except OSError:
        raise AuthorizationError("credential file could not be read") from None


def _credential_token(credentials: GoogleCredentials) -> str:
    access_token = credentials.token
    if not isinstance(access_token, str) or not access_token.strip():
        raise AuthorizationError("credential refresh returned no access token")
    return access_token


def _credential_principal(credentials: GoogleCredentials) -> str | None:
    for attribute in ("service_account_email", "signer_email"):
        principal = getattr(credentials, attribute, None)
        if isinstance(principal, str) and principal.strip() and principal != "default":
            return principal
    information = credentials.get_cred_info()
    if information is not None:
        principal = information.get("principal")
        if isinstance(principal, str) and principal.strip():
            return principal
    return None


def _credential_expiry(credentials: GoogleCredentials) -> datetime | None:
    if credentials.expiry is None:
        return None
    return _as_utc(credentials.expiry)


def _reject_service_account_key(credentials: GoogleCredentials) -> None:
    if isinstance(credentials, ServiceAccountKeyCredentials):
        raise AuthorizationError(
            "service-account key credentials are not a supported source"
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
