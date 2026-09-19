"""Shared pytest fixtures.

Sets fake AUTHENTIK_URL/AUTHENTIK_API_TOKEN *before* importing server.py,
since the module requires both at import time (see server._require_env).
Individual tests then swap server.client's transport to control what
"Authentik" returns, via httpx.MockTransport — no extra mocking library
needed, it ships in httpx (already a runtime dependency).
"""

import os

os.environ.setdefault("AUTHENTIK_URL", "http://test-authentik:9000")
os.environ.setdefault("AUTHENTIK_API_TOKEN", "test-api-token")

import httpx  # noqa: E402
import pytest  # noqa: E402

import server  # noqa: E402


@pytest.fixture
def mock_authentik(monkeypatch):
    """Point server.client at a fake Authentik.

    Usage:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={...})
        mock_authentik(handler)
    """

    def _install(handler):
        fake_client = httpx.Client(
            base_url=f"{server.AUTHENTIK_URL}/api/v3",
            headers={"Authorization": f"Bearer {server.AUTHENTIK_API_TOKEN}"},
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(server, "client", fake_client)
        return fake_client

    return _install


@pytest.fixture
def no_auth(monkeypatch):
    """Run with MCP_AUTH_TOKEN unset (the default, open-server mode)."""
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", None)


@pytest.fixture
def with_auth(monkeypatch):
    """Run with a known MCP_AUTH_TOKEN, and return it."""
    token = "test-shared-secret"
    monkeypatch.setattr(server, "MCP_AUTH_TOKEN", token)
    return token


@pytest.fixture
def writes_enabled(monkeypatch):
    """Run with AUTHENTIK_ALLOW_WRITES=true (the default, in tests, is False)."""
    monkeypatch.setattr(server, "AUTHENTIK_ALLOW_WRITES", True)
