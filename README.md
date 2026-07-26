# sum_server

Authoritative backend for OpenSUM: ownership, inventory, audit. Owns the database, exposes the HTTP API, serves the web UI, records the audit log.

## Layout

`src/sum_server/` is feature-organized:

- `core/` — cross-cutting (settings, db, errors, logging, audit, security primitives)
- `auth/` — user sessions and agent tokens
- `users/`, `teams/`, `servers/`, `components/`, `agents/`, `audit/` — domain modules
- `api/v1.py` — router aggregator mounted at `/api/v1`
- `main.py` — FastAPI app factory + lifespan

`alembic/` holds DB migrations. `tests/` mirrors the package tree.

## Setup

Requires Python 3.12+ and a running PostgreSQL.

```sh
uv sync                                       # install deps (incl. dev with --extra dev)
cp .env.example .env                          # then edit values
uv run alembic upgrade head                   # apply migrations
uv run uvicorn sum_server.main:app --reload
```

Once running:

- `GET /healthz` — liveness
- `GET /readyz` — readiness (DB + signing key)
- `GET /docs` — OpenAPI / Swagger UI
- `GET /.well-known/sum-server-signing-key` — Ed25519 public key for agents

## Testing

```sh
uv run pytest                                 # all
uv run pytest tests/unit                      # fast subset
uv run pytest -k <pattern>                    # targeted
```

Tests use `testcontainers-python` to bring up an ephemeral PostgreSQL.

Pre-alpha. Expect schema churn until the protocol shapes stabilize.

## In-place updates (optional)

The Settings page can check GitHub for new releases and update the server in place, with rollback on failure. This requires the server to run as **root** under **systemd** (it shells out to `git`, `uv`, `alembic`, `systemctl`, `pg_dump`/`pg_restore`, and launches an out-of-process updater via `systemd-run`). Set:

```sh
SUM_SERVER_INSTALL_DIR=/opt/sum_server        # the git checkout this runs from
SUM_SERVER_DATA_DIR=/var/lib/sum-server       # DB dumps + cached agent binaries
SUM_SERVER_SERVICE_NAME=sum-server            # the systemd unit to restart
SUM_SERVER_UV_BIN=/root/.local/bin/uv         # optional; see below
```

The updater runs in its own transient systemd unit, which gets the systemd default `PATH` (no `~/.local/bin`). If `uv` was installed with the standard installer it will not be found there, so set `SUM_SERVER_UV_BIN` to its absolute path. The Settings panel refuses the update up front and tells you when `uv` cannot be located, rather than failing partway through and rolling back.

You do **not** need to duplicate the other settings for the updater: the transient unit inherits nothing from the server process, so the server writes its effective configuration to `$SUM_SERVER_DATA_DIR/updater.env` (mode `0600`) and passes it to the unit explicitly. This works whether the service is configured through systemd `Environment=`, an `EnvironmentFile=`, or a `.env`.

Without `INSTALL_DIR` (or when not root), the Settings panel shows self-update disabled with the reason — nothing else is affected. Agent updates are served from `DATA_DIR` and triggered per host from the host pages.
