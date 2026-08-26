"""Tests for credential source parsing and lease resolution."""

import json
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import execution.credentials as credential_module
from core.errors import AuthorizationError, DataConsistencyError
from core.tracing import DebugTrace
from execution.credentials import (
    CredentialLease,
    CredentialResolver,
    CredentialSourceError,
    CredentialSourceKind,
    GoogleAccessTokenInspector,
    TokenMetadata,
    parse_credential_source,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
PRINCIPAL = "runner@authorized-project.iam.gserviceaccount.com"
ACCESS_TOKEN = "synthetic-short-lived-access-token"


class StaticTokenInspector:
    """Return fixed metadata while recording the inspected value."""

    def __init__(
        self,
        principal: str = PRINCIPAL,
        expires_at: datetime = NOW + timedelta(minutes=10),
    ) -> None:
        self.metadata = TokenMetadata(principal=principal, expires_at=expires_at)
        self.inspected: list[str] = []

    def inspect(self, access_token: str) -> TokenMetadata:
        self.inspected.append(access_token)
        return self.metadata


class FakeGoogleCredentials:
    """Small google-auth credential fake for offline resolver tests."""

    def __init__(
        self,
        *,
        access_token: str = ACCESS_TOKEN,
        expiry: datetime | None = NOW + timedelta(minutes=10),
        principal: str | None = PRINCIPAL,
    ) -> None:
        self.token = access_token
        self.expiry = expiry
        self.service_account_email = principal
        self.refreshed = False

    def refresh(self, request: object) -> None:
        self.refreshed = True

    def get_cred_info(self) -> None:
        return None


@pytest.mark.parametrize(
    ("descriptor", "kind", "locator"),
    [
        ("stdin", CredentialSourceKind.STDIN, None),
        ("file:/protected/token", CredentialSourceKind.FILE, "/protected/token"),
        ("env:VERISENTINEL_TOKEN", CredentialSourceKind.ENV, "VERISENTINEL_TOKEN"),
        ("adc", CredentialSourceKind.ADC, None),
        (
            f"impersonate:{PRINCIPAL}",
            CredentialSourceKind.IMPERSONATE,
            PRINCIPAL,
        ),
    ],
)
def test_credential_source_parser_accepts_only_supported_forms(
    descriptor: str,
    kind: CredentialSourceKind,
    locator: str | None,
) -> None:
    source = parse_credential_source(descriptor)

    assert source.kind == kind
    assert source.locator == locator


@pytest.mark.parametrize(
    "descriptor",
    [
        "",
        ACCESS_TOKEN,
        "file:",
        "env:",
        "env:NOT-AN-ENVIRONMENT-NAME",
        "adc:unexpected",
        "stdin:unexpected",
        "impersonate:user@example.com",
        "unknown:value",
    ],
)
def test_credential_source_parser_rejects_invalid_forms(descriptor: str) -> None:
    with pytest.raises(CredentialSourceError) as raised:
        parse_credential_source(descriptor)

    assert ACCESS_TOKEN not in str(raised.value)


@pytest.mark.parametrize("kind", [CredentialSourceKind.STDIN, CredentialSourceKind.ENV])
def test_supplied_sources_resolve_verified_lease(kind: CredentialSourceKind) -> None:
    inspector = StaticTokenInspector()
    resolver = CredentialResolver(
        environ={"VERISENTINEL_TOKEN": ACCESS_TOKEN},
        stdin_reader=lambda: ACCESS_TOKEN,
        token_inspector=inspector,
        now=lambda: NOW,
    )
    descriptor = "stdin" if kind == CredentialSourceKind.STDIN else "env:VERISENTINEL_TOKEN"
    resolver.register("run/default", parse_credential_source(descriptor))

    lease = resolver.resolve("run/default", PRINCIPAL)

    assert lease.principal == PRINCIPAL
    assert lease.source_kind == kind
    assert lease.access_token == ACCESS_TOKEN
    assert inspector.inspected == [ACCESS_TOKEN]
    assert ACCESS_TOKEN not in repr(lease)
    with pytest.raises(TypeError):
        json.dumps(lease)
    lease.close()
    assert lease.closed
    with pytest.raises(AuthorizationError, match="closed"):
        _ = lease.access_token


def test_protected_file_source_resolves_without_exposing_path(tmp_path: Path) -> None:
    credential_path = tmp_path / "credential"
    credential_path.write_text(ACCESS_TOKEN, encoding="utf-8")
    credential_path.chmod(0o600)
    inspector = StaticTokenInspector()
    resolver = CredentialResolver(token_inspector=inspector, now=lambda: NOW)
    source = parse_credential_source(f"file:{credential_path}")
    resolver.register("run/default", source)

    with resolver.resolve("run/default", PRINCIPAL) as lease:
        assert lease.access_token == ACCESS_TOKEN

    assert lease.closed
    assert str(credential_path) not in repr(source)
    assert credential_path.read_text(encoding="utf-8") == ACCESS_TOKEN


def test_file_source_rejects_broad_permissions(tmp_path: Path) -> None:
    credential_path = tmp_path / "credential"
    credential_path.write_text(ACCESS_TOKEN, encoding="utf-8")
    credential_path.chmod(0o640)
    resolver = CredentialResolver(
        token_inspector=StaticTokenInspector(),
        now=lambda: NOW,
    )
    resolver.register(
        "run/default",
        parse_credential_source(f"file:{credential_path}"),
    )

    with pytest.raises(AuthorizationError, match="permissions") as raised:
        resolver.resolve("run/default", PRINCIPAL)

    assert ACCESS_TOKEN not in str(raised.value)
    assert str(credential_path) not in str(raised.value)


def test_missing_credential_file_fails_without_exposing_path(tmp_path: Path) -> None:
    credential_path = tmp_path / "missing-credential"
    resolver = CredentialResolver(
        token_inspector=StaticTokenInspector(),
        now=lambda: NOW,
    )
    resolver.register(
        "run/default",
        parse_credential_source(f"file:{credential_path}"),
    )

    with pytest.raises(AuthorizationError, match="could not be read") as raised:
        resolver.resolve("run/default", PRINCIPAL)

    assert str(credential_path) not in str(raised.value)


def test_missing_and_empty_sources_fail_without_inspection() -> None:
    inspector = StaticTokenInspector()
    resolver = CredentialResolver(
        environ={},
        token_inspector=inspector,
        now=lambda: NOW,
    )

    with pytest.raises(AuthorizationError, match="no configured source"):
        resolver.resolve("run/missing", PRINCIPAL)
    resolver.register("run/default", parse_credential_source("env:VERISENTINEL_TOKEN"))
    with pytest.raises(AuthorizationError, match="contains no access token"):
        resolver.resolve("run/default", PRINCIPAL)
    assert not inspector.inspected


def test_inspector_failure_cannot_put_token_in_error_or_trace(tmp_path: Path) -> None:
    class EchoingInspector:
        def inspect(self, access_token: str) -> TokenMetadata:
            raise RuntimeError(f"inspection failed for {access_token}")

    resolver = CredentialResolver(
        environ={"TOKEN": ACCESS_TOKEN},
        token_inspector=EchoingInspector(),
        now=lambda: NOW,
    )
    resolver.register("run/default", parse_credential_source("env:TOKEN"))

    with pytest.raises(AuthorizationError) as raised:
        resolver.resolve("run/default", PRINCIPAL)
    rendered_error = "".join(traceback.format_exception(raised.value))
    trace_path = tmp_path / "trace.jsonl"
    trace = DebugTrace(trace_path, "run", echo=False)
    trace.emit("execution", "credential_failed", error=rendered_error)

    assert ACCESS_TOKEN not in rendered_error
    assert ACCESS_TOKEN not in trace_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("principal", "expires_at", "message"),
    [
        ("different@example.com", NOW + timedelta(minutes=10), "principal"),
        (PRINCIPAL, NOW - timedelta(seconds=1), "expired"),
        (PRINCIPAL, NOW + timedelta(seconds=30), "too close"),
    ],
)
def test_invalid_token_metadata_fails_closed(
    principal: str,
    expires_at: datetime,
    message: str,
) -> None:
    resolver = CredentialResolver(
        environ={"TOKEN": ACCESS_TOKEN},
        token_inspector=StaticTokenInspector(principal, expires_at),
        now=lambda: NOW,
    )
    resolver.register("run/default", parse_credential_source("env:TOKEN"))

    with pytest.raises(AuthorizationError, match=message) as raised:
        resolver.resolve("run/default", PRINCIPAL)

    assert ACCESS_TOKEN not in str(raised.value)


def test_reference_cannot_be_rebound_to_another_source() -> None:
    resolver = CredentialResolver()
    resolver.register("run/default", parse_credential_source("stdin"))

    with pytest.raises(DataConsistencyError, match="cannot be rebound"):
        resolver.register("run/default", parse_credential_source("adc"))


def test_adc_refreshes_and_verifies_principal_without_network() -> None:
    credentials = FakeGoogleCredentials()
    requested_scopes: list[tuple[str, ...]] = []

    def load_adc(*, scopes: tuple[str, ...]) -> tuple[FakeGoogleCredentials, str]:
        requested_scopes.append(scopes)
        return credentials, "project"

    resolver = CredentialResolver(
        adc_loader=load_adc,
        request_factory=object,
        token_inspector=StaticTokenInspector(),
        now=lambda: NOW,
    )
    resolver.register("run/default", parse_credential_source("adc"))

    lease = resolver.resolve("run/default", PRINCIPAL)

    assert credentials.refreshed
    assert lease.access_token == ACCESS_TOKEN
    assert requested_scopes and "cloud-platform" in requested_scopes[0][0]


def test_adc_rejects_service_account_key_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = FakeGoogleCredentials()
    monkeypatch.setattr(
        credential_module,
        "ServiceAccountKeyCredentials",
        FakeGoogleCredentials,
    )

    def load_adc(*, scopes: tuple[str, ...]) -> tuple[FakeGoogleCredentials, str]:
        return credentials, "project"

    resolver = CredentialResolver(adc_loader=load_adc, now=lambda: NOW)
    resolver.register("run/default", parse_credential_source("adc"))

    with pytest.raises(AuthorizationError, match="key credentials"):
        resolver.resolve("run/default", PRINCIPAL)

    assert not credentials.refreshed


def test_impersonation_uses_exact_target_and_short_lifetime() -> None:
    source_credentials = FakeGoogleCredentials(principal="operator@example.com")
    target_credentials = FakeGoogleCredentials(principal=PRINCIPAL)
    captured: dict[str, object] = {}

    def load_adc(*, scopes: tuple[str, ...]) -> tuple[FakeGoogleCredentials, str]:
        return source_credentials, "project"

    def impersonate(**arguments: object) -> FakeGoogleCredentials:
        captured.update(arguments)
        return target_credentials

    resolver = CredentialResolver(
        adc_loader=load_adc,
        impersonated_factory=impersonate,
        request_factory=object,
        now=lambda: NOW,
    )
    resolver.register(
        "run/default",
        parse_credential_source(f"impersonate:{PRINCIPAL}"),
    )

    lease = resolver.resolve("run/default", PRINCIPAL)

    assert lease.principal == PRINCIPAL
    assert target_credentials.refreshed
    assert captured["source_credentials"] is source_credentials
    assert captured["target_principal"] == PRINCIPAL
    assert captured["lifetime"] == 3600


def test_impersonation_rejects_principal_mismatch_before_adc() -> None:
    called = False

    def load_adc(*, scopes: tuple[str, ...]) -> tuple[FakeGoogleCredentials, str]:
        nonlocal called
        called = True
        return FakeGoogleCredentials(), "project"

    resolver = CredentialResolver(adc_loader=load_adc, now=lambda: NOW)
    resolver.register(
        "run/default",
        parse_credential_source(f"impersonate:{PRINCIPAL}"),
    )

    with pytest.raises(AuthorizationError, match="target"):
        resolver.resolve("run/default", "other@project.iam.gserviceaccount.com")
    assert not called


def test_google_inspector_posts_token_in_body_and_returns_metadata() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert ACCESS_TOKEN not in str(request.url)
        assert f"access_token={ACCESS_TOKEN}" in request.content.decode()
        return httpx.Response(
            200,
            json={
                "email": PRINCIPAL,
                "exp": str(int((NOW + timedelta(minutes=10)).timestamp())),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        metadata = GoogleAccessTokenInspector(client).inspect(ACCESS_TOKEN)

    assert metadata.principal == PRINCIPAL
    assert metadata.expires_at == NOW + timedelta(minutes=10)


def test_google_inspector_translates_failure_without_token() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(AuthorizationError) as raised,
    ):
        GoogleAccessTokenInspector(client).inspect(ACCESS_TOKEN)

    assert ACCESS_TOKEN not in str(raised.value)


def test_credential_lease_context_always_closes_on_failure() -> None:
    lease = CredentialLease(
        principal=PRINCIPAL,
        expires_at=NOW + timedelta(minutes=10),
        source_kind=CredentialSourceKind.ENV,
        _access_token=ACCESS_TOKEN,
    )

    with pytest.raises(RuntimeError, match="provider failure"), lease:
        raise RuntimeError("provider failure")

    assert lease.closed
