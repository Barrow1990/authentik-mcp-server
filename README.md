# authentik-mcp-server

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that
connects to [Authentik](https://goauthentik.io), packaged for Docker.

It runs as a standing network service (streamable-http transport, not stdio),
so any MCP client on your internal network can connect to
`http://<host>:<port>/mcp` — the container isn't spawned per-client, and
container lifecycle/updates can be handed off to a tool like
[Dockhand](https://dockhand.pro).

## Tools

| Tool | Description |
|---|---|
| `list_users` | List users, optionally filtered by search term and/or active status |
| `user_details` | Full details for one user by ID |
| `list_groups` | List groups, optionally filtered by name |
| `list_applications` | List configured SSO applications |
| `recent_events` | Recent audit/auth events (logins, failures, config changes, ...) |
| `set_user_active` | Enable or disable a user account |
| `system_status` | Authentik version and runtime info |

### Why no destructive tools

Authentik is an identity provider — it holds real user accounts, group
membership, and SSO/application configuration for everything else behind it.
A wrong or malicious tool call here has a much bigger blast radius than one
against a media manager, so this server is deliberately read-mostly:

- Every tool is read-only **except** `set_user_active`, which only flips a
  user's enabled/disabled flag. That's reversible (flip it back) and covers
  the actual homelab use case ("disable this account") without exposing
  anything sharper.
- There is **no** create/delete tool for users, groups, applications,
  providers, or flows, and no tool that touches password hashes, tokens, or
  recovery links, even though Authentik's API supports all of that
  (`set_password`, `recovery`, `impersonate`, etc. on the users endpoint).
  If you need those, use Authentik's own admin UI.
- `recent_events` is read-only but still sensitive — audit events can include
  IPs and usernames tied to real login activity. Scope who can reach this
  server's `/mcp` endpoint accordingly (see Authentication, below).

If you want more capability than this, treat it as a deliberate decision to
widen scope, not a missing feature — add tools individually and mind the
permission each one needs (see Configuration below).

## Health endpoints

Two plain HTTP endpoints, reachable without `MCP_AUTH_TOKEN` (so Docker's
`HEALTHCHECK`, Dockhand, or any other monitor can poll them without the
secret):

| Endpoint | Checks | Healthy | Unhealthy |
|---|---|---|---|
| `GET /health` | The process is up and serving HTTP. Does **not** call Authentik. | `200 {"status": "ok"}` | (doesn't respond) |
| `GET /ready` | `AUTHENTIK_URL` is reachable and `AUTHENTIK_API_TOKEN` is valid (via `GET /core/users/me/`, which any valid token can call regardless of its other permissions). | `200 {"status": "ok", "reachable": true, "authenticated": true, "authentik": {...}}` | `503 {"status": "error", "reachable": ..., "authenticated": ..., "error": "..."}` |

They're split deliberately: `/health` is what the container's own
`HEALTHCHECK` uses (so a transient Authentik outage doesn't get the
container itself restarted in a loop), while `/ready` is for verifying
config — after changing `AUTHENTIK_URL`/`AUTHENTIK_API_TOKEN`,
`curl http://<host>:8937/ready` tells you plainly whether the host is
reachable, the token is valid, or both.

`/ready` deliberately checks token *validity*, not the specific permissions
individual tools need. `system_status` needs the
`authentik_rbac.view_system_info` permission and `set_user_active` needs
write access to users; a `403` from either of those is a permission-scope
problem with the token, not a readiness failure — `/ready` will still report
healthy as long as the token can authenticate at all.

## Authentication

Set `MCP_AUTH_TOKEN` (a random shared secret — `openssl rand -hex 32`) and
every request must carry `Authorization: Bearer <token>` or the server
returns `401`. This is checked by a small Starlette middleware in front of
the MCP app, **not** the `mcp` SDK's built-in OAuth support
(`mcp.server.auth`) — that machinery expects a full OAuth authorization
server (issuer/resource metadata, RFC 8414/8707/9068 discovery), which is
unnecessary complexity for one secret shared by trusted LAN clients.

Leave `MCP_AUTH_TOKEN` unset and the server runs with **no auth** — anything
that can reach `http://<host>:<port>/mcp` can call every tool, including
`set_user_active` and `list_users`. Given what this server has access to
(every user account in your identity provider), running without
`MCP_AUTH_TOKEN` is a materially bigger risk here than for the media-manager
MCP servers in this same family — set it. The server logs a warning on
startup when it's running without one. Either way, the trust boundary is
still the network:

- **Do not** publish this port through any reverse proxy, port-forward, or
  anything else reachable from outside your LAN/VLAN — the bearer token
  protects against anyone *on* the network, not against the open internet.
- Bind the compose `ports:` mapping to a specific internal interface (e.g.
  `192.168.1.50:8937:8937`) rather than all interfaces, if you want to be
  stricter about which hosts on your network can reach it at all.

### AUTHENTIK_API_TOKEN scope

Create a dedicated service account in Authentik (Directory > Users > Create
Service Account) rather than reusing your own admin user, then create its
API token under Directory > Tokens and App passwords (Intent: API Token).
Grant that service account only the RBAC permissions the tools you actually
use need — at minimum `authentik_core.view_user` for `list_users`/
`user_details`, plus `view_group`/`view_application`/`view_event` for the
other read tools, `authentik_core.change_user` if you want `set_user_active`
to work, and `authentik_rbac.view_system_info` for `system_status`. A full
superuser token works too but grants far more than this server needs.

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `AUTHENTIK_URL` | yes | — | e.g. `http://192.168.1.50:9000` (or your reverse-proxied HTTPS URL) |
| `AUTHENTIK_API_TOKEN` | yes | — | Bearer token from Directory > Tokens and App passwords |
| `MCP_HOST` | no | `0.0.0.0` | Interface the server binds to inside the container |
| `MCP_PORT` | no | `8937` | Port the server listens on |
| `MCP_AUTH_TOKEN` | no | — | Shared secret required as `Authorization: Bearer <token>`. Unset = no auth (see above) |

Authentik's REST API is a fixed path prefix (`/api/v3/...`) rather than
content-negotiated or runtime-discoverable the way the Servarr apps
(Sonarr/Radarr, also in this family of MCP servers) expose a `GET /api`
endpoint reporting current/deprecated versions. There's no
`AUTHENTIK_API_VERSION` setting here because there's nothing to point it
at — `v3` is hardcoded into `server.py`'s HTTP client, and a future
Authentik `v4` would need a code change here. Unlike the Servarr-family
servers, `/ready` can't detect that drift automatically; if tool calls start
404ing after an Authentik upgrade, check Authentik's release notes for an
API version bump.

## Image

Built and pushed to `ghcr.io/barrow1990/authentik-mcp-server` by
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) on every push to
`main` that passes tests, tagged `:latest` and `:<commit-sha>`.
`docker-compose.yml` pulls `:latest` by default; swap in `build: .` there
instead if you'd rather build locally from the `Dockerfile`.

The image is a three-stage build: `builder` compiles dependencies into
`--target=/deps` (all of them, including `cryptography`'s compiled `cffi`
extension, ship musllinux wheels, so this needs no compiler even on alpine);
`prep` starts fresh from `python:3.12-alpine`, drops pip/setuptools/wheel,
strips stdlib pieces this headless server never touches (`tkinter`,
`idlelib`, `lib2to3`, `ensurepip`, ...), adds the non-root `app` user, and
copies in `/deps` and `server.py`; `runtime` then does a single
`COPY --from=prep / /` onto a `scratch` base. That last step matters more
than it looks — a plain `RUN rm -rf` only *hides* files still physically
present in the base image's own layers underneath, so it doesn't shrink a
normal layered image at all; copying the already-trimmed filesystem onto
`scratch` is what actually drops those bytes from what gets pushed.

That takes the published image to roughly **~98MB**, the same floor as the
other servers in this family — `mcp`'s own dependency graph pulls in
`cryptography`'s AES-GCM/HKDF regardless. Dependencies in `requirements.txt`
are pinned to exact versions rather than `>=` ranges, so a routine
`docker build` can't silently pull in a heavier resolution than the one that
was actually tested.

## Running with Docker Compose

```bash
cp .env.example .env   # fill in AUTHENTIK_URL / AUTHENTIK_API_TOKEN
docker compose up -d --pull always
```

The server is then reachable at `http://<docker-host>:8937/mcp` from anything
on your internal network.

## Managing with Dockhand

Point Dockhand at `ghcr.io/barrow1990/authentik-mcp-server` and let it track
new tags — this is the registry-pull model Dockhand's image-update tracking
(Grype/Trivy scans, tag tracking, scheduled updates) is actually built
around. The alternative, pointing Dockhand at this repo as a Git-deployed
Compose stack with `build: .`, works too, but syncing new Git commits does
**not** imply rebuilding the image — those are two separate steps for a
build-from-source stack.

**Make the GHCR package public**, or every pull will need `docker login
ghcr.io` with a PAT on each deploy host — a private package by default
requires auth even to `docker pull`, which most homelab boxes won't have
configured.

Set a restart policy of `unless-stopped` (already in `docker-compose.yml`) so
Dockhand-driven restarts and host reboots bring it back up without manual
intervention. The `HEALTHCHECK` in the `Dockerfile` (`GET /health`) drives
Docker's/Dockhand's container health status; use `GET /ready` (see above)
separately if you want to alert on Authentik connectivity specifically
rather than container liveness.

**Environment variables in Dockhand**: `docker-compose.yml` loads
`AUTHENTIK_URL`/`AUTHENTIK_API_TOKEN`/`MCP_AUTH_TOKEN` via
`env_file: [.env, .env.dockhand]` (both optional; `.env.dockhand` loads
second, so it wins for any key it also sets). This is deliberate — a
Git-deployed stack's `.env` is whatever's checked out from the repo (i.e.
`.env.example`'s placeholders, since real `.env` is gitignored and not
committed), while Dockhand writes the values you configure in its UI to
`.env.dockhand` instead. If you set `AUTHENTIK_URL` in Dockhand's UI and the
container is still using a placeholder, check that Dockhand is actually
writing to `.env.dockhand` in the stack directory (not some other file) and
that a rebuild has run since — a synced Git file change alone doesn't
rebuild the image; see `GET /ready` to confirm what's live.

## Connecting a client

### Claude Code

```bash
claude mcp add authentik -s user --transport http http://<docker-host>:8937/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```
(Drop the `--header` flag if you're running with `MCP_AUTH_TOKEN` unset.)

### Claude Desktop

Claude Desktop's built-in config expects a locally-spawned `command`, so for
a network server like this you'll need an HTTP-to-stdio bridge such as
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "authentik": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "http://<docker-host>:8937/mcp",
        "--header", "Authorization: Bearer <MCP_AUTH_TOKEN>"
      ]
    }
  }
}
```

## Running without Docker

```bash
pip install -r requirements.txt
AUTHENTIK_URL=http://192.168.1.50:9000 AUTHENTIK_API_TOKEN=your-api-token \
MCP_AUTH_TOKEN=your-shared-secret python server.py
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

- `tests/test_tools.py` — each tool's logic against a mocked Authentik
  (`httpx.MockTransport`, no extra mocking library needed).
- `tests/test_http.py` — `/health`, `/ready`, and the bearer-auth middleware,
  via `server.build_app()` (the exact app `__main__` runs) through Starlette's
  `TestClient`.
- `tests/test_live_authentik.py` — **opt-in** contract tests against a real
  Authentik instance, to catch drift if an Authentik upgrade renames/removes
  a field these tools depend on (`pk`, `username`, `is_active`, `action`,
  ...). Skipped by default (no Authentik in CI); run with:
  ```bash
  RUN_LIVE_AUTHENTIK_TESTS=1 AUTHENTIK_URL=https://authentik.example.com \
  AUTHENTIK_API_TOKEN=<real token> python -m pytest tests/test_live_authentik.py -v
  ```

CI (`.github/workflows/ci.yml`) runs the mocked suite on every push/PR; the
GHCR build only runs after it passes.

## License

MIT
