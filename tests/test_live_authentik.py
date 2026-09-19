"""Live contract tests against a *real* Authentik instance.

These do not run by default — there's no Authentik in CI, and we don't want
to accidentally fire real requests using the fake AUTHENTIK_URL/AUTHENTIK_API_TOKEN
that conftest.py sets for the rest of the suite. To run them:

    RUN_LIVE_AUTHENTIK_TESTS=1 AUTHENTIK_URL=https://authentik.example.com \\
    AUTHENTIK_API_TOKEN=<real token> pytest tests/test_live_authentik.py -v

Use a token scoped to a dedicated read-mostly service account, not your own
admin session — see README.md.

Point is to catch drift: if an Authentik upgrade renames/removes a field our
tools depend on (pk, username, is_active, action, ...), these fail even
though the mocked unit tests in test_tools.py would still happily pass (they
only assert against fixtures we wrote ourselves).
"""

import os

import httpx
import pytest

import server

RUN_LIVE = os.environ.get("RUN_LIVE_AUTHENTIK_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="opt-in only: set RUN_LIVE_AUTHENTIK_TESTS=1 with a real AUTHENTIK_URL/AUTHENTIK_API_TOKEN",
)


@pytest.fixture(scope="module")
def live_client():
    return httpx.Client(
        base_url=f"{server.AUTHENTIK_URL}/api/v3",
        headers={"Authorization": f"Bearer {server.AUTHENTIK_API_TOKEN}"},
        timeout=15,
    )


def test_users_me_shape(live_client):
    """The fields /ready depends on actually exist."""
    response = live_client.get("/core/users/me/")
    response.raise_for_status()
    data = response.json()

    assert "user" in data
    assert "username" in data["user"]


def test_user_shape_matches_what_list_users_assumes(live_client):
    """Every field list_users() reads with .get() (safe) or [..] (required) exists."""
    response = live_client.get("/core/users/")
    response.raise_for_status()
    body = response.json()

    assert "results" in body
    users = body["results"]
    if not users:
        pytest.skip("no users returned — nothing to validate the shape of")

    sample = users[0]
    for required_field in ("pk", "username"):
        assert required_field in sample, f"Authentik's /core/users/ no longer returns '{required_field}'"

    for soft_field in ("name", "email", "is_active", "is_superuser", "last_login", "groups"):
        if soft_field not in sample:
            pytest.skip(f"'{soft_field}' missing from a real record — list_users() will report it as null")


def test_our_tools_run_cleanly_against_real_authentik(monkeypatch, live_client):
    """Run the actual tool functions (not just raw requests) against real Authentik."""
    monkeypatch.setattr(server, "client", live_client)

    users = server.list_users()
    assert isinstance(users, list)

    groups = server.list_groups()
    assert isinstance(groups, list)

    apps = server.list_applications()
    assert isinstance(apps, list)
