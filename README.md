# authentik-mcp-server

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server that
connects to [Authentik](https://goauthentik.io), packaged for Docker.

It runs as a standing network service (streamable-http transport, not stdio),
so any MCP client on your internal network can connect to
`http://<host>:<port>/mcp` — the container isn't spawned per-client, and
container lifecycle/updates can be handed off to a tool like
[Dockhand](https://dockhand.pro).

## Two modes, one image

This image can run as either of two containers, chosen by `AUTHENTIK_MCP_MODE`:

| Mode | Port (default) | Tools |
|---|---|---|
| `read` (default) | 8937 | Everything except `set_user_active` |
| `write` | 8942 | Everything `read` has, **plus** `set_user_active` |

This is not a config toggle guarding one shared codebase — in `read` mode,
`set_user_active` is never defined at all, so it can't appear in a tool
listing or be called under any circumstances. `docker-compose.yml` runs
`read` always-on and puts `write` behind a Compose profile, so a plain
`docker compose up -d` only ever starts something that structurally cannot
write to your identity provider. Starting the write container is a
deliberate, separate action: `docker compose --profile write up -d`.

## Tools

| Tool | Mode | Description |
|---|---|---|
| `list_users` | read, write | List users, optionally filtered by search term and/or active status |
| `user_details` | read, write | Full details for one user by ID |
| `list_groups` | read, write | List groups, optionally filtered by name |
| `list_applications` | read, write | List configured SSO applications |
| `recent_events` | read, write | Recent audit/auth events (logins, failures, config changes, ...) |
| `system_status` | read, write | Authentik version and runtime info |
| `set_user_active` | **write only** | Enable or disable a user account |

### Why no destructive tools

Authentik is an identity provider — it holds real user accounts, group
membership, and SSO/application configuration for everything else behind it.
A wrong or malicious tool call here has a much bigger blast radius than one
against a media manager, so this server is deliberately read-mostly, with
writes isolated behind several independent layers rather than one flag:

1. **Mode split (structural).** `set_user_active` only exists in write-mode
   containers. Point a client at the read-mode server and the tool is
   genuinely absent — not hidden, not permission-denied, just not there.
2. **`AUTHENTIK_ALLOW_WRITES=true`** — even in a write-mode container, this
   must be explicitly set (default `false`). An operator has to turn writes
   on for that specific deployment.
3. **`confirm=True`** on the call itself — an assistant has to explicitly
   ask for the write every time; it can't happen from a misread instruction
   or a config left at its default.
4. **Separate credentials recommended** (see Configuration below): give the
   read and write containers different, separately-scoped Authentik API
   tokens and different `MCP_AUTH_TOKEN`s, so a compromise of the read
   container's environment doesn't hand over write-capable credentials too.

Beyond `set_user_active`, there is **no** create/delete tool for users,
groups, applications, providers, or flows, and no tool that touches password
hashes, tokens, or recovery links, even though Authentik's API supports all
of that (`set_password`, `recovery`, `impersonate`, etc. on the users
endpoint). If you need those, use Authentik's own admin UI.

`recent_events` is read-only but still sensitive — audit events can include
IPs and usernames tied to real login activity. Scope who can reach either
server's `/mcp` endpoint accordingly (see Authentication, below).

If you want more capability than this, treat it as a deliberate decision to
widen scope, not a missing feature — add tools individually, decide which
mode they belong in, and mind the permission each one needs.

## Health endpoints

Two plain HTTP endpoints per container, reachable without `MCP_AUTH_TOKEN`
(so Docker's `HEALTHCHECK`, Dockhand, or any other monitor can poll them
without the secret):

| Endpoint | Checks | Healthy | Unhealthy |
|---|---|---|---|
| `GET /health` | The process is up and serving HTTP. Does **not** call Authentik. | `200 {"status": "ok"}` | (doesn't respond) |
| `GET /ready` | `AUTHENTIK_URL` is reachable and the mode's API token is valid (via `GET /core/users/me/`, which any valid token can call regardless of its other permissions). | `200 {"status": "ok", "reachable": true, "authenticated": true, "mode": "read"\|"write", "authentik": {...}}` | `503 {"status": "error", "reachable": ..., "authenticated": ..., "error": "..."}` |

They're split deliberately: `/health` is what each container's own
`HEALTHCHECK` uses (so a transient Authentik outage doesn't get the
container itself restarted in a loop), while `/ready` is for verifying
config — `curl http://<host>:8937/ready` (read) or
`curl http://<host>:8942/ready` (write) tells you plainly whether that
container's host is reachable, its token is valid, or both. The response's
`mode` field confirms which container answered.

`/ready` deliberately checks token *validity*, not the specific permissions
individual tools need. `system_status` needs the
`authentik_rbac.view_system_info` permission and `set_user_active` needs
write access to users; a `403` from either of those is a permission-scope
problem with that container's token, not a readiness failure — `/ready` will
still report healthy as long as the token can authenticate at all.

## Authentication

Set `MCP_AUTH_TOKEN_READ` and `MCP_AUTH_TOKEN_WRITE` (random shared secrets —
`openssl rand -hex 32` each) and every request to the corresponding
container must carry `Authorization: Bearer <that container's token>` or it
returns `401`. Use **two different values** — that's what stops a leaked
read-side bearer token from being replayed against the write container's
port. This is checked by a small Starlette middleware in front of the MCP
app, **not** the `mcp` SDK's built-in OAuth support (`mcp.server.auth`) —
that machinery expects a full OAuth authorization server (issuer/resource
metadata, RFC 8414/8707/9068 discovery), which is unnecessary complexity for
a secret shared by trusted LAN clients.

Leave a container's token unset and **that container** runs with no auth —
anything that can reach its `/mcp` endpoint can call every tool it exposes.
For the write container that includes `set_user_active` (still behind
`AUTHENTIK_ALLOW_WRITES` + `confirm=True`, but still — set the token). Each
container logs a warning on startup when it's running without one. Either
way, the trust boundary is still the network:

- **Do not** publish either port through any reverse proxy, port-forward, or
  anything else reachable from outside your LAN/VLAN.
- Bind the compose `ports:` mapping to a specific internal interface (e.g.
  `192.168.1.50:8937:8937`) rather than all interfaces, if you want to be
  stricter about which hosts on your network can reach it at all.

### Authentik API token scope — use two, not one

Create **two separate** service accounts in Authentik (Directory > Users >
Create Service Account) rather than reusing your own admin user, or reusing
one account for both modes. Create each one's API token under Directory >
Tokens and App passwords (Intent: API Token):

- **Read token** (`AUTHENTIK_API_TOKEN_READ`): grant only `view_user`,
  `view_group`, `view_application`, `view_event`, and
  `authentik_rbac.view_system_info` — whatever the read tools you actually
  use need. This account should have **no** write permissions on anything.
- **Write token** (`AUTHENTIK_API_TOKEN_WRITE`): the above, plus
  `authentik_core.change_user` for `set_user_active`.

Don't rely on assumed defaults for what a fresh service account or API
token can and can't do — Authentik's permission model has changed across
versions (see the RBAC/permissions docs for your installed version), and
what a token can do depends on the role/group it's actually assigned, not
just on it being newly created. Check the token's effective permissions
directly in Authentik's admin UI after creating it, for both accounts.

This is the layer that actually matters most: even a full compromise of the
read container's process — its environment, its memory, everything — can't
yield a credential capable of writing anything, because Authentik's own RBAC
enforces that, not just this server's tool registry. A single
`AUTHENTIK_API_TOKEN` (no `_READ`/`_WRITE` suffix) is supported as a fallback
if you'd rather use one token for both, but that gives up this guarantee.

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `AUTHENTIK_MCP_MODE` | no | `read` | `read` or `write` — see "Two modes, one image" above |
| `AUTHENTIK_URL` | yes | — | e.g. `http://192.168.1.50:9000` (or your reverse-proxied HTTPS URL) |
| `AUTHENTIK_API_TOKEN_READ` | yes, in read mode | — | Falls back to plain `AUTHENTIK_API_TOKEN` if unset |
| `AUTHENTIK_API_TOKEN_WRITE` | yes, in write mode | — | Falls back to plain `AUTHENTIK_API_TOKEN` if unset |
| `AUTHENTIK_ALLOW_WRITES` | no | `false` | Write-mode-only master switch for `set_user_active`. Must be `true` *and* the call must pass `confirm=True` |
| `MCP_HOST` | no | `0.0.0.0` | Interface the server binds to inside the container |
| `MCP_PORT` | no | `8937` (read) / `8942` (write) | Port the server listens on |
| `MCP_AUTH_TOKEN_READ` / `MCP_AUTH_TOKEN_WRITE` | no | — | Per-mode bearer secret. Falls back to plain `MCP_AUTH_TOKEN` if unset. Unset entirely = no auth on that container (see above) |

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
`main` that passes tests, tagged `:latest` and `:<commit-sha>`. The same
image serves both modes — `docker-compose.yml` runs it twice with different
`AUTHENTIK_MCP_MODE` values. Swap in `build: .` there instead if you'd
rather build locally from the `Dockerfile`.

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
cp .env.example .env   # fill in AUTHENTIK_URL and both API tokens
docker compose up -d --pull always              # read container only
docker compose --profile write up -d            # also starts the write container
```

The read server is reachable at `http://<docker-host>:8937/mcp`; the write
server, once started, at `http://<docker-host>:8942/mcp`.

## One `.env`, but no container sees more than its own variables

`docker-compose.yml` deliberately does **not** use `env_file:` (which would
dump the whole `.env` into every container). Instead each service's
`environment:` block names its own variables explicitly via `${VAR}`
substitution — a variable never referenced in a service's block simply never
reaches that container. Verify this yourself any time after editing `.env`:

```bash
docker compose --profile write config
```

`authentik-mcp-read`'s resolved environment should show `AUTHENTIK_API_TOKEN_READ`
but never `AUTHENTIK_API_TOKEN_WRITE` or `MCP_AUTH_TOKEN_WRITE` — confirmed
during development of this compose file, but re-check it after any edit,
since a typo in a service's `environment:` block would silently reintroduce
exactly the leak this design exists to prevent.

### Does this fit how Dockhand expects to be used?

**Only if Dockhand feeds `docker compose` a plain `.env`.** `${VAR}`
substitution reads exclusively from a literal `.env` in the project
directory (or `--env-file <path>` / real shell environment variables) —
never from an arbitrarily-named file. This is a change from how earlier
servers in this family (sonarr, radarr, ...) load config — those use
`env_file: [.env, .env.dockhand]` on the understanding that Dockhand writes
its UI-configured values to a separate `.env.dockhand`, which `env_file:`
(unlike substitution) can load regardless of filename.

This repo hasn't been verified against a live Dockhand deployment. Before
relying on it under Dockhand:

1. Deploy this stack in Dockhand and check whether the values you set in
   its UI actually reach the containers — `docker exec authentik-mcp-read env`.
2. If they don't (Dockhand is writing to `.env.dockhand`, substitution never
   sees it, and the `:?` markers in `docker-compose.yml` make `docker
   compose up` fail outright rather than start with empty credentials): edit
   `.env` directly in this repo's checkout instead of through Dockhand's UI,
   or check whether Dockhand's stack settings let you point it at `.env`
   instead of `.env.dockhand`.

**Make the GHCR package public**, or every pull will need `docker login
ghcr.io` with a PAT on each deploy host — a private package by default
requires auth even to `docker pull`, which most homelab boxes won't have
configured.

Set a restart policy of `unless-stopped` (already in `docker-compose.yml`) so
Dockhand-driven restarts and host reboots bring the read container back up
without manual intervention. The write container, being profile-gated, needs
its profile invoked again after a host reboot too — that's intentional, not
a bug to fix; it's meant to require a deliberate action to bring back.

## Connecting a client

### Claude Code

```bash
claude mcp add authentik -s user --transport http http://<docker-host>:8937/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN_READ>"
```

Add the write server as a **separate**, explicitly-named connection only
when you actually want it available, rather than always-on alongside the
read one:

```bash
claude mcp add authentik-write -s user --transport http http://<docker-host>:8942/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN_WRITE>"
```
(Drop the `--header` flag on either if you're running that container with
its token unset.)

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
        "--header", "Authorization: Bearer <MCP_AUTH_TOKEN_READ>"
      ]
    }
  }
}
```

## Running without Docker

```bash
pip install -r requirements.txt
AUTHENTIK_URL=http://192.168.1.50:9000 AUTHENTIK_MCP_MODE=read \
AUTHENTIK_API_TOKEN_READ=your-read-scoped-token \
MCP_AUTH_TOKEN_READ=your-shared-secret python server.py
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

- `tests/test_tools.py` — each tool's logic against a mocked Authentik
  (`httpx.MockTransport`, no extra mocking library needed). Runs with
  `AUTHENTIK_MCP_MODE=write` (the superset) so `set_user_active` is covered
  alongside everything read mode has.
- `tests/test_http.py` — `/health`, `/ready`, and the bearer-auth middleware,
  via `server.build_app()` (the exact app `__main__` runs) through Starlette's
  `TestClient`.
- `tests/test_mode.py` — the mode split itself, via subprocess (this
  behavior only shows up at *import* time, so it can't be exercised by
  monkeypatching an already-imported module): read mode genuinely lacking
  `set_user_active`, write mode having it, per-mode port defaults, invalid
  `AUTHENTIK_MCP_MODE` values exiting non-zero, and the `_READ`/`_WRITE`
  credential fallback logic.
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
