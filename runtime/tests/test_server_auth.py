from __future__ import annotations

import httpx
import pytest

from app.server import auth
from app.server import main as server
from app.sessions.store import SessionStore


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", SessionStore(path=tmp_path / "s.sqlite"))

    def make(token: str) -> httpx.AsyncClient:
        monkeypatch.setattr(auth, "RUNTIME_TOKEN", token)
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return make


@pytest.mark.asyncio
async def test_daemon_status_is_open_even_when_token_configured(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/daemon/status")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_token_configured_allows_all_requests(client_factory) -> None:
    async with client_factory("") as client:
        response = await client.get("/v1/sessions")
    assert response.status_code == 200


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


def test_is_authorized_open_when_no_token_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "RUNTIME_TOKEN", "")
    assert auth.is_authorized("") is True
    assert auth.is_authorized("Bearer anything") is True


def test_is_authorized_rejects_malformed_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "RUNTIME_TOKEN", "secret")
    assert auth.is_authorized("secret") is False  # 缺少 "Bearer " 前缀
    assert auth.is_authorized("") is False
