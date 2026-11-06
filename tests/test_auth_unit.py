"""Unit tests for credential handling: LDAP client, login service and settings.

No LDAP server, database or Redis needed.
"""

from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.exceptions import AuthenticationError
from app.ldap import LDAPClient, _escape_ldap_filter_chars
from app.service import UserService

pytestmark = pytest.mark.unit


def _client() -> LDAPClient:
    return LDAPClient(
        ldap_url="ldap://ldap.example.com",
        admin_username="svc-reader@example.com",
        admin_password="service-account-password",
        search_bases=["OU=Users,DC=example,DC=com"],
    )


class RecordingConnection:
    """Stands in for ldap3.Connection and records how it was opened."""

    def __init__(self, record, user, password):
        record.append((user, password))
        self.bound = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def opened(monkeypatch):
    """Patch ldap3.Connection; returns the list of (user, password) binds."""
    import ldap3

    record = []
    monkeypatch.setattr(
        ldap3,
        "Connection",
        lambda server, user=None, password=None, **kw: RecordingConnection(record, user, password),
    )
    monkeypatch.setattr(ldap3, "Server", lambda url, **kw: url)
    return record


@pytest.mark.parametrize("password", ["", None])
def test_empty_password_never_binds(opened, monkeypatch, password):
    client = _client()
    monkeypatch.setattr(client, "_sync_search", lambda **kw: [object()])

    assert client._sync_authenticate("jdoe", password) is False
    assert opened == []


def test_user_bind_never_falls_back_to_service_account(opened):
    client = _client()
    client._get_connection(user="jdoe@example.com", password="")

    assert opened == [("jdoe@example.com", "")]


def test_service_account_used_only_without_credentials(opened):
    _client()._get_connection()

    assert opened == [("svc-reader@example.com", "service-account-password")]


def test_valid_password_binds_as_the_user(opened, monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_sync_search", lambda **kw: [object()])

    assert client._sync_authenticate("jdoe", "user-password") is True
    assert opened == [("jdoe@example.com", "user-password")]


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("*", r"\2a"),
        ("admin)(objectClass=*", r"admin\29\28objectClass=\2a"),
        ("a\\b", r"a\5cb"),
        ("nul\x00", r"nul\00"),
    ],
)
def test_filter_values_are_escaped(raw, escaped):
    assert _escape_ldap_filter_chars(raw) == escaped
    assert f"(sAMAccountName={escaped})" in _client()._build_filter(raw, None)


@pytest.mark.parametrize(("identifier", "password"), [("admin", ""), ("", "secret-pass")])
async def test_service_rejects_empty_credentials_before_any_backend(identifier, password):
    ldap = SimpleNamespace(is_available=True)
    service = UserService(repo=None, cache=None, ldap=ldap)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("a backend was called with empty credentials")

    service._authenticate_ldap = must_not_run
    service._authenticate_database = must_not_run

    with pytest.raises(AuthenticationError):
        await service.authenticate(identifier, password)


def test_login_form_rejects_empty_password(client):
    response = client.post(
        f"{settings.api_prefix}/login", data={"username": "admin", "password": ""}
    )
    assert response.status_code == 422


def test_email_service_url_accepts_new_and_legacy_names(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    monkeypatch.setenv("EMAIL_SERVICE_URL", "http://email.example.com/api/v1/emails")
    assert Settings().email_service_url == "http://email.example.com/api/v1/emails"

    monkeypatch.delenv("EMAIL_SERVICE_URL")
    monkeypatch.setenv("MAILSENDER_URL", "http://legacy.example.com/api/v1/emails")
    assert Settings().email_service_url == "http://legacy.example.com/api/v1/emails"


def test_settings_have_no_admin_default():
    assert not hasattr(settings, "admin_password")


def test_lifespan_provides_the_email_http_client(client):
    """get_email_client reads app.state.http_client; the lifespan must create it."""
    import httpx
    from starlette.requests import Request

    from app.dependencies import get_email_client

    assert isinstance(client.app.state.http_client, httpx.AsyncClient)
    request = Request({"type": "http", "app": client.app, "headers": []})
    assert get_email_client(request).url == settings.email_service_url
