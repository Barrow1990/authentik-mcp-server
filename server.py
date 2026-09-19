"""
Authentik MCP Server

Exposes a small, deliberately read-mostly set of Authentik (identity
provider / SSO) operations as MCP tools, so an MCP-compatible AI assistant
(e.g. Claude Code / Claude Desktop) can look up users, groups, applications,
and recent auth events, and check system status via Authentik's REST API.

This image runs in one of two MODES, chosen at startup by AUTHENTIK_MCP_MODE
(default "read" — the safe default). Both modes come from this same file and
image; which one a given container is depends entirely on this one variable:

  read   Every tool except set_user_active. This is the only mode where the
         write tool doesn't exist — not gated, not hidden, genuinely absent
         from this process's tool registry, so it can't be discovered or
         called no matter what a client asks for.
  write  Everything read mode has, PLUS set_user_active — itself still
         gated by AUTHENTIK_ALLOW_WRITES + confirm=True (see below). This
         mode exists so a deployment can choose to run it only when a write
         is actually intended, rather than have every deployment carry
         write capability by default.

Run both from the same image as two separate containers (see
docker-compose.yml) — normal day-to-day tool use only ever talks to the read
container; the write container is a deliberate, separate thing to stand up
(or leave running behind its own bearer token) only when you actually want
an assistant to be able to flip a user's active flag.

Configuration is via environment variables:
  AUTHENTIK_MCP_MODE        "read" or "write" (default "read")
  AUTHENTIK_URL             e.g. http://192.168.1.50:9000 (required)
  AUTHENTIK_API_TOKEN_READ  token used when AUTHENTIK_MCP_MODE=read
  AUTHENTIK_API_TOKEN_WRITE token used when AUTHENTIK_MCP_MODE=write
  AUTHENTIK_API_TOKEN       fallback used if the mode-specific variable
                            above isn't set (simplest option if you're happy
                            with one token covering both modes; see below
                            for why two separately-scoped tokens are safer)
  AUTHENTIK_ALLOW_WRITES    write-mode-only master switch for set_user_active
                            (default false — see below)
  MCP_HOST                  interface to bind to (default 0.0.0.0)
  MCP_PORT                  port to listen on (default 8937 in read mode,
                            8942 in write mode)
  MCP_AUTH_TOKEN_READ / MCP_AUTH_TOKEN_WRITE / MCP_AUTH_TOKEN
                            shared secret required as `Authorization: Bearer
                            <token>` on every request, resolved the same way
                            as the AUTHENTIK_API_TOKEN_* family above
                            (optional — if none is set, the server is open
                            to anyone who can reach it; see README)

Recommended setup: create two separate Authentik service accounts, one
scoped to view_* permissions only (used as AUTHENTIK_API_TOKEN_READ) and one
that also has authentik_core.change_user (used as AUTHENTIK_API_TOKEN_WRITE).
That way even a full compromise of the read container's process (env vars,
memory, whatever) never yields a credential capable of writing anything —
the separation is enforced by Authentik's own RBAC, not just by this code
choosing not to register a tool. Two distinct MCP_AUTH_TOKEN_* values
similarly mean a leaked read-side bearer secret can't be replayed against
the write container's port at all.

Authentik's API is versioned as a fixed path prefix (/api/v3/...), not
content-negotiated or discoverable at runtime the way the Servarr apps
(Sonarr/Radarr) expose a GET /api endpoint reporting current/deprecated
versions — there is no equivalent here, so `v3` is simply hardcoded into
the client's base_url below. A future Authentik v4 would need a code change
in this file; there's no automated drift detection for it the way
sonarr-mcp-server/radarr-mcp-server have for their API version.

`set_user_active`, in write mode, is additionally gated by two independent
opt-ins on top of the mode split above, neither of which is on by default:
  1. AUTHENTIK_ALLOW_WRITES=true in the environment (server-level: the
     operator running this specific container has to deliberately turn
     writes on, even though it's already the write-mode image).
  2. confirm=True passed on the tool call itself (call-level: an assistant
     can't trigger it via a misread instruction or a stale default; the
     caller has to explicitly ask for the write, every time).
Both are required. Missing either raises rather than silently no-op'ing, so
a caller gets a clear reason instead of a confusing non-effect.

Transport: streamable-http. This runs as a standing network service (bind
0.0.0.0 inside the container; publish the port only on your internal
network/VLAN — never forward it externally) rather than being spawned
per-client over stdio, so any MCP client on the LAN can connect to
http://<host>:<port>/mcp.

Auth here is a single shared bearer token checked by plain middleware, not
the SDK's built-in OAuth support (mcp.server.auth) — that machinery expects
a full OAuth authorization server (issuer/resource metadata, RFC 8414/8707/
9068 discovery), which is unwarranted complexity for a single internal
secret shared by trusted LAN clients.
"""

import hmac
import os
import sys

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: required environment variable {name} is not set", file=sys.stderr)
        sys.exit(1)
    return value


def _mode_scoped_env(base_name: str, mode: str) -> str | None:
    """Resolve `{base_name}_{MODE}` first, falling back to plain `{base_name}`.

    Lets a deployment give the read and write containers genuinely different
    credentials/secrets (recommended — see module docstring) while still
    working with just one plain variable for anyone who doesn't need that
    separation."""
    return os.environ.get(f"{base_name}_{mode.upper()}") or os.environ.get(base_name)


def _require_mode_scoped_env(base_name: str, mode: str) -> str:
    value = _mode_scoped_env(base_name, mode)
    if not value:
        print(
            f"error: required environment variable {base_name}_{mode.upper()} "
            f"(or {base_name}) is not set",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


AUTHENTIK_MCP_MODE = os.environ.get("AUTHENTIK_MCP_MODE", "read").lower()
if AUTHENTIK_MCP_MODE not in ("read", "write"):
    print(f"error: AUTHENTIK_MCP_MODE must be 'read' or 'write', got {AUTHENTIK_MCP_MODE!r}", file=sys.stderr)
    sys.exit(1)

AUTHENTIK_URL = _require_env("AUTHENTIK_URL").rstrip("/")
AUTHENTIK_API_TOKEN = _require_mode_scoped_env("AUTHENTIK_API_TOKEN", AUTHENTIK_MCP_MODE)
AUTHENTIK_ALLOW_WRITES = os.environ.get("AUTHENTIK_ALLOW_WRITES", "").lower() in ("1", "true", "yes")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8937" if AUTHENTIK_MCP_MODE == "read" else "8942"))
MCP_AUTH_TOKEN = _mode_scoped_env("MCP_AUTH_TOKEN", AUTHENTIK_MCP_MODE)

client = httpx.Client(
    base_url=f"{AUTHENTIK_URL}/api/v3",
    headers={"Authorization": f"Bearer {AUTHENTIK_API_TOKEN}"},
    timeout=30,
)

mcp = MCPServer(f"authentik-{AUTHENTIK_MCP_MODE}")


@mcp.tool()
def list_users(search: str | None = None, is_active: bool | None = None) -> list[dict]:
    """List Authentik users, optionally filtered by a search term (matches username/name/email) and/or active status."""
    params = {}
    if search:
        params["search"] = search
    if is_active is not None:
        params["is_active"] = is_active

    response = client.get("/core/users/", params=params)
    response.raise_for_status()
    users = response.json()["results"]

    return [
        {
            "id": u["pk"],
            "username": u["username"],
            "name": u.get("name"),
            "email": u.get("email"),
            "isActive": u.get("is_active"),
            "isSuperuser": u.get("is_superuser"),
            "lastLogin": u.get("last_login"),
            "groups": u.get("groups", []),
        }
        for u in users
    ]


@mcp.tool()
def user_details(user_id: int) -> dict:
    """Get full details for a single Authentik user by their ID."""
    response = client.get(f"/core/users/{user_id}/")
    response.raise_for_status()
    return response.json()


@mcp.tool()
def list_groups(name: str | None = None) -> list[dict]:
    """List Authentik groups, optionally filtered by a name substring."""
    response = client.get("/core/groups/")
    response.raise_for_status()
    groups = response.json()["results"]

    if name:
        needle = name.lower()
        groups = [g for g in groups if needle in g["name"].lower()]

    return [
        {
            "id": g["pk"],
            "name": g["name"],
            "isSuperuser": g.get("is_superuser"),
            "userCount": len(g.get("users", []) or []),
        }
        for g in groups
    ]


@mcp.tool()
def list_applications(search: str | None = None) -> list[dict]:
    """List SSO applications configured in Authentik, optionally filtered by a search term."""
    params = {"only_with_launch_url": False}
    if search:
        params["search"] = search

    response = client.get("/core/applications/", params=params)
    response.raise_for_status()
    apps = response.json()["results"]

    return [
        {
            "slug": a["slug"],
            "name": a["name"],
            "provider": a.get("provider"),
            "launchUrl": a.get("meta_launch_url") or a.get("launch_url"),
            "group": a.get("group"),
        }
        for a in apps
    ]


@mcp.tool()
def recent_events(username: str | None = None, action: str | None = None, limit: int = 50) -> list[dict]:
    """List recent Authentik audit/auth events (logins, failures, config changes, ...), most recent first."""
    params = {"ordering": "-created", "page_size": limit}
    if username:
        params["username"] = username
    if action:
        params["action"] = action

    response = client.get("/events/events/", params=params)
    response.raise_for_status()
    events = response.json()["results"]

    return [
        {
            "action": e.get("action"),
            "user": (e.get("user") or {}).get("username"),
            "clientIp": e.get("client_ip"),
            "created": e.get("created"),
            "app": e.get("app"),
        }
        for e in events
    ]


# set_user_active only exists as an attribute of this module — let alone as
# a registered MCP tool — when AUTHENTIK_MCP_MODE=write. In read mode there
# is no code path that defines it at all, so it can't be listed, discovered,
# or called regardless of what a client sends; this isn't an extra check
# guarding the function, it's the function simply not existing.
if AUTHENTIK_MCP_MODE == "write":

    @mcp.tool()
    def set_user_active(user_id: int, is_active: bool, confirm: bool = False) -> str:
        """Enable or disable an Authentik user account. Reversible — does not delete anything.

        Guarded: requires AUTHENTIK_ALLOW_WRITES=true in the server's environment
        AND confirm=True on this call. Both are off by default; call with
        confirm=True only once you actually mean to flip this user's access."""
        if not AUTHENTIK_ALLOW_WRITES:
            raise PermissionError(
                "set_user_active is disabled: set AUTHENTIK_ALLOW_WRITES=true in the "
                "server's environment to enable write tools on this server"
            )
        if not confirm:
            raise ValueError(
                "set_user_active requires confirm=True — this is a deliberate second "
                "guard on top of AUTHENTIK_ALLOW_WRITES, not a bug"
            )

        response = client.patch(f"/core/users/{user_id}/", json={"is_active": is_active})
        response.raise_for_status()
        state = "enabled" if is_active else "disabled"
        return f"User {user_id} {state}"


@mcp.tool()
def system_status() -> dict:
    """Get Authentik system/version information. Requires a token with the authentik_rbac.view_system_info permission."""
    response = client.get("/admin/system/")
    response.raise_for_status()
    info = response.json()
    return {
        "version": (info.get("runtime") or {}).get("authentik_version"),
        "platform": (info.get("runtime") or {}).get("platform"),
        "serverTime": info.get("server_time"),
        "httpIsSecure": info.get("http_is_secure"),
    }


# Paths that must stay reachable without MCP_AUTH_TOKEN, so Docker's own
# HEALTHCHECK, Dockhand's health probe, etc. don't need the secret.
UNAUTHENTICATED_PATHS = {"/health", "/ready"}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    """Liveness check: the process is up and serving HTTP. Does not call Authentik."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> Response:
    """Readiness check: AUTHENTIK_URL is reachable and AUTHENTIK_API_TOKEN is valid.

    Calls GET /core/users/me/ — any valid API token can call this regardless
    of what permissions/scopes it otherwise holds, so this checks "is the
    token valid at all" without requiring the broader permissions some tools
    here need (e.g. system_status needs authentik_rbac.view_system_info,
    set_user_active needs write access to users — a 403 from one of those
    tools is a permission-scope problem, not a readiness failure)."""
    try:
        response = client.get("/core/users/me/", timeout=5)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        reason = "invalid Authentik API token" if status_code in (401, 403) else f"Authentik returned HTTP {status_code}"
        return JSONResponse(
            {"status": "error", "reachable": True, "authenticated": status_code not in (401, 403), "error": reason},
            status_code=503,
        )
    except httpx.RequestError as error:
        return JSONResponse(
            {
                "status": "error",
                "reachable": False,
                "authenticated": False,
                "error": f"cannot reach Authentik at {AUTHENTIK_URL}: {error}",
            },
            status_code=503,
        )

    whoami = response.json().get("user", {})
    return JSONResponse(
        {
            "status": "ok",
            "reachable": True,
            "authenticated": True,
            "mode": AUTHENTIK_MCP_MODE,
            "authentik": {"url": AUTHENTIK_URL, "username": whoami.get("username")},
        }
    )


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require `Authorization: Bearer <MCP_AUTH_TOKEN>` on every request except
    the health/readiness endpoints, which are meant to be publicly pollable."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in UNAUTHENTICATED_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, MCP_AUTH_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    """Build the ASGI app (routes + auth middleware). Split out from __main__ so
    tests can exercise the real, fully-wired app without going through uvicorn."""
    app = mcp.streamable_http_app(host=MCP_HOST)

    if MCP_AUTH_TOKEN:
        app.add_middleware(BearerTokenMiddleware)
        print("Auth enabled: Authorization: Bearer <token> required", file=sys.stderr)
    else:
        print("WARNING: MCP_AUTH_TOKEN not set — server is open to anyone who can reach it", file=sys.stderr)

    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host=MCP_HOST, port=MCP_PORT)
