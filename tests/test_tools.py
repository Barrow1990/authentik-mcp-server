"""Unit tests for each MCP tool's logic, against a mocked Authentik.

`@mcp.tool()` returns the original function unchanged, so these call the
tools directly as plain Python functions — no MCP protocol/session machinery
involved here (that's covered separately in test_http.py).
"""

import json

import httpx
import pytest

import server

SAMPLE_USERS = {
    "pagination": {"count": 2},
    "results": [
        {
            "pk": 1,
            "username": "chris",
            "name": "Chris",
            "email": "chris@example.com",
            "is_active": True,
            "is_superuser": True,
            "last_login": "2026-09-01T00:00:00Z",
            "groups": ["admins"],
        },
        {
            "pk": 2,
            "username": "svc-radarr",
            "name": "svc-radarr",
            "email": None,
            "is_active": True,
            "is_superuser": False,
            "last_login": None,
            "groups": [],
        },
    ],
}


def test_list_users_returns_shaped_records(mock_authentik):
    mock_authentik(lambda req: httpx.Response(200, json=SAMPLE_USERS))

    result = server.list_users()

    assert len(result) == 2
    assert result[0] == {
        "id": 1,
        "username": "chris",
        "name": "Chris",
        "email": "chris@example.com",
        "isActive": True,
        "isSuperuser": True,
        "lastLogin": "2026-09-01T00:00:00Z",
        "groups": ["admins"],
    }


def test_list_users_passes_search_and_is_active_params(mock_authentik):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search"] == "chris"
        assert request.url.params["is_active"] == "true"
        return httpx.Response(200, json={"pagination": {"count": 0}, "results": []})

    mock_authentik(handler)

    server.list_users(search="chris", is_active=True)


def test_list_users_propagates_http_errors(mock_authentik):
    mock_authentik(lambda req: httpx.Response(500, json={"detail": "boom"}))

    with pytest.raises(httpx.HTTPStatusError):
        server.list_users()


def test_user_details_hits_correct_path(mock_authentik):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/core/users/42/"
        return httpx.Response(200, json={"pk": 42, "username": "chris"})

    mock_authentik(handler)

    result = server.user_details(42)

    assert result == {"pk": 42, "username": "chris"}


def test_list_groups_filters_by_name_case_insensitive(mock_authentik):
    mock_authentik(
        lambda req: httpx.Response(
            200,
            json={
                "pagination": {"count": 2},
                "results": [
                    {"pk": "g1", "name": "Admins", "is_superuser": True, "users": [1]},
                    {"pk": "g2", "name": "Media Users", "is_superuser": False, "users": [1, 2]},
                ],
            },
        )
    )

    result = server.list_groups(name="admin")

    assert len(result) == 1
    assert result[0] == {"id": "g1", "name": "Admins", "isSuperuser": True, "userCount": 1}


def test_list_applications_shapes_records(mock_authentik):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/core/applications/"
        return httpx.Response(
            200,
            json={
                "pagination": {"count": 1},
                "results": [
                    {
                        "slug": "sonarr",
                        "name": "Sonarr",
                        "provider": 3,
                        "meta_launch_url": "https://sonarr.example.com",
                        "launch_url": None,
                        "group": "",
                    }
                ],
            },
        )

    mock_authentik(handler)

    result = server.list_applications()

    assert result == [
        {
            "slug": "sonarr",
            "name": "Sonarr",
            "provider": 3,
            "launchUrl": "https://sonarr.example.com",
            "group": "",
        }
    ]


def test_recent_events_hits_correct_path_and_params(mock_authentik):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/events/events/"
        assert request.url.params["ordering"] == "-created"
        assert request.url.params["page_size"] == "10"
        assert request.url.params["username"] == "chris"
        return httpx.Response(
            200,
            json={
                "pagination": {"count": 1},
                "results": [
                    {
                        "action": "login",
                        "user": {"username": "chris"},
                        "client_ip": "10.0.0.5",
                        "created": "2026-09-18T10:00:00Z",
                        "app": "authentik.core",
                    }
                ],
            },
        )

    mock_authentik(handler)

    result = server.recent_events(username="chris", limit=10)

    assert result == [
        {
            "action": "login",
            "user": "chris",
            "clientIp": "10.0.0.5",
            "created": "2026-09-18T10:00:00Z",
            "app": "authentik.core",
        }
    ]


def test_set_user_active_sends_patch(mock_authentik, writes_enabled):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/v3/core/users/7/"
        assert json.loads(request.content) == {"is_active": False}
        return httpx.Response(200, json={"pk": 7, "is_active": False})

    mock_authentik(handler)

    result = server.set_user_active(7, False, confirm=True)

    assert result == "User 7 disabled"


def test_set_user_active_blocked_without_allow_writes(mock_authentik):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("set_user_active must not call Authentik when writes are disabled")

    mock_authentik(handler)

    with pytest.raises(PermissionError, match="AUTHENTIK_ALLOW_WRITES"):
        server.set_user_active(7, False, confirm=True)


def test_set_user_active_blocked_without_confirm(mock_authentik, writes_enabled):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("set_user_active must not call Authentik without confirm=True")

    mock_authentik(handler)

    with pytest.raises(ValueError, match="confirm=True"):
        server.set_user_active(7, False)


def test_system_status_shapes_runtime_info(mock_authentik):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/admin/system/"
        return httpx.Response(
            200,
            json={
                "runtime": {"authentik_version": "2026.1.0", "platform": "linux"},
                "server_time": "2026-09-18T10:00:00Z",
                "http_is_secure": False,
            },
        )

    mock_authentik(handler)

    result = server.system_status()

    assert result == {
        "version": "2026.1.0",
        "platform": "linux",
        "serverTime": "2026-09-18T10:00:00Z",
        "httpIsSecure": False,
    }
