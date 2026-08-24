from __future__ import annotations

import httpx
import pytest

from app.server import auth
from app.server import main as server
from tests.fakes import build_test_runtime


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    # httpx.ASGITransport does not run the ASGI lifespan, so the runtime is
    # injected through dependency_overrides rather than app.state.
    runtime = build_test_runtime(tmp_path)
    server.app.dependency_overrides[server.get_runtime] = lambda: runtime
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    monkeypatch.delenv(auth.ANONYMOUS_ENV, raising=False)

    def make(token: str = "", *, allow_anonymous: bool = False) -> httpx.AsyncClient:
        # Configure through the environment rather than by patching a module
        # constant, so these tests exercise the real configuration path.
        if token:
            monkeypatch.setenv(auth.TOKEN_ENV, token)
        else:
            monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
        if allow_anonymous:
            monkeypatch.setenv(auth.ANONYMOUS_ENV, "1")
        else:
            monkeypatch.delenv(auth.ANONYMOUS_ENV, raising=False)
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    yield make

    server.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_daemon_status_is_open_even_when_token_configured(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/daemon/status")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_missing_token_configuration_fails_closed(client_factory) -> None:
    """An unconfigured token must not mean "no authentication required".

    Any local process could otherwise forge approvals and accept an edit or a
    risky shell command on the user's behalf.
    """
    async with client_factory("") as client:
        response = await client.get("/v1/sessions")

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert auth.TOKEN_ENV in detail
    assert auth.ANONYMOUS_ENV in detail, "the refusal must tell the user how to opt in"


@pytest.mark.asyncio
async def test_anonymous_access_requires_explicit_opt_in(client_factory) -> None:
    async with client_factory("", allow_anonymous=True) as client:
        response = await client.get("/v1/sessions")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_configured_token_takes_precedence_over_anonymous_opt_in(client_factory) -> None:
    """The opt-in is for "no token configured", not a way to bypass a real token."""
    async with client_factory("secret", allow_anonymous=True) as client:
        response = await client.get("/v1/sessions")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rejects_request_missing_authorization_header(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/sessions")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rejects_wrong_token(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/sessions", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_accepts_correct_token(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/sessions", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200


def test_is_authorized_fails_closed_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    monkeypatch.delenv(auth.ANONYMOUS_ENV, raising=False)
    assert auth.is_authorized("") is False
    assert auth.is_authorized("Bearer anything") is False


def test_is_authorized_honours_anonymous_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    monkeypatch.setenv(auth.ANONYMOUS_ENV, "1")
    assert auth.is_authorized("") is True


def test_is_authorized_rejects_malformed_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.TOKEN_ENV, "secret")
    assert auth.is_authorized("secret") is False  # Missing the "Bearer " prefix.
    assert auth.is_authorized("") is False


def test_token_is_read_per_request_not_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import-time capture would make the module order-dependent and prevent
    reconfiguring the Runtime inside the ASGI lifespan."""
    monkeypatch.setenv(auth.TOKEN_ENV, "first")
    assert auth.is_authorized("Bearer first") is True
    monkeypatch.setenv(auth.TOKEN_ENV, "second")
    assert auth.is_authorized("Bearer first") is False
    assert auth.is_authorized("Bearer second") is True
