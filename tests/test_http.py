"""Tests for the wired-together ASGI app: /health, /ready, and the bearer-auth
middleware — via server.build_app(), the same function __main__ uses."""

import httpx
from starlette.testclient import TestClient

import server


def test_health_does_not_call_authentik(mock_authentik, no_auth):
    def handler(request):
        raise AssertionError("/health must not call Authentik")

    mock_authentik(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_success(mock_authentik, no_auth):
    mock_authentik(lambda req: httpx.Response(200, json={"user": {"username": "svc-mcp"}}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 200
    assert body == {
        "status": "ok",
        "reachable": True,
        "authenticated": True,
        "authentik": {"url": server.AUTHENTIK_URL, "username": "svc-mcp"},
    }


def test_ready_invalid_api_token(mock_authentik, no_auth):
    mock_authentik(lambda req: httpx.Response(401, json={"detail": "Authentication credentials were not provided."}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is False
    assert "invalid Authentik API token" in body["error"]


def test_ready_forbidden_token_reported_as_invalid(mock_authentik, no_auth):
    mock_authentik(lambda req: httpx.Response(403, json={"detail": "forbidden"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["authenticated"] is False


def test_ready_unreachable_host(mock_authentik, no_auth):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    mock_authentik(handler)

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is False
    assert body["authenticated"] is False


def test_ready_other_authentik_error(mock_authentik, no_auth):
    mock_authentik(lambda req: httpx.Response(500, json={"detail": "boom"}))

    with TestClient(server.build_app()) as client:
        response = client.get("/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert "HTTP 500" in body["error"]


def test_no_auth_token_leaves_mcp_open(mock_authentik, no_auth):
    mock_authentik(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    # No 401 — auth is off. The exact protocol response doesn't matter here,
    # only that the request wasn't rejected by the auth layer.
    assert response.status_code != 401


def test_auth_token_blocks_mcp_without_header(mock_authentik, with_auth):
    mock_authentik(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get("/mcp", headers={"Accept": "application/json, text/event-stream"})

    assert response.status_code == 401


def test_auth_token_blocks_mcp_with_wrong_token(mock_authentik, with_auth):
    mock_authentik(lambda req: httpx.Response(200, json={}))

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer wrong-token",
            },
        )

    assert response.status_code == 401


def test_auth_token_allows_mcp_with_correct_token(mock_authentik, with_auth):
    mock_authentik(lambda req: httpx.Response(200, json={}))
    token = with_auth

    with TestClient(server.build_app()) as client:
        response = client.get(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
        )

    # Past auth — the normal MCP protocol response for a bare GET with no
    # prior session (400 missing-session), not a 401.
    assert response.status_code != 401


def test_auth_token_does_not_block_health_or_ready(mock_authentik, with_auth):
    mock_authentik(lambda req: httpx.Response(200, json={"user": {"username": "svc-mcp"}}))

    with TestClient(server.build_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
