# garmin-reasoner

Personal service that receives Garmin Connect data via webhooks, stores it as raw JSON in
SQLite, and exposes it to Claude through a remote MCP server. See [CLAUDE.md](CLAUDE.md) for
the architecture and build plan.

## Routes

| Route | Exposure | Purpose |
| --- | --- | --- |
| `POST /garmin/webhook` | Funnel (public) | Garmin push/ping notifications |
| `GET /callback` | Funnel | Garmin OAuth redirect |
| `/mcp` | Funnel | MCP server (streamable HTTP), bearer-token protected |
| `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/*`, `/mcp-login` | Funnel | OAuth server for the claude.ai connector |
| `GET /connect` | tailnet only | Start linking your Garmin account (admin password) |
| `POST /admin/backfill` | tailnet only | Request historical data, e.g. `{"summary_types": ["dailies"], "start": "2026-09-01", "end": "2026-09-30"}` |
| `POST /admin/gap-check` | tailnet only | Backfill recently missed days now (also runs daily) |
| `POST /admin/disconnect` | tailnet only | Deregister from Garmin and forget tokens (data is kept) |
| `GET /healthz` | container only | Health check |

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env    # fill in values
uv sync                 # install dependencies
uv run pytest           # run tests
uv run ruff check .     # lint
uv run uvicorn garmin_reasoner.main:create_app --factory --reload
```

## Deploy (home server)

1. **Configure.** `cp .env.example .env` and fill it in. `PUBLIC_BASE_URL` is the server's
   Funnel URL (`https://<host>.<tailnet>.ts.net`). `ADMIN_PASSWORD` must be at least 16
   characters. It guards the public `/mcp-login` page, so use a generated one.
2. **Run.**

   ```sh
   mkdir -p data backups && sudo chown 10001 data backups
   docker compose up -d --build
   docker compose logs -f
   ```

   The container listens on `127.0.0.1:8000` only.
3. **Expose via Tailscale.** `sh deploy/tailscale.sh`. This puts the public paths on Funnel
   (port 443) and `/connect` + `/admin` on tailnet-only Serve (port 8443). Check with
   `tailscale funnel status`: only the public paths should be listed under Funnel.
4. **Garmin developer portal.**
   - Redirect URI: `https://<host>.<tailnet>.ts.net/callback` (same as `REDIRECT_URI`).
   - Endpoint configuration: for each enabled summary type, set the URL to
     `https://<host>.<tailnet>.ts.net/garmin/webhook` and pick push or ping (both work).
5. **Link Garmin.** From a device on the tailnet, open
   `https://<host>.<tailnet>.ts.net:8443/connect`, log in with any username and the admin
   password, and approve on Garmin. You should see `{"status": "connected", ...}`.
6. **Load history.** Request a backfill for each type you want, up to 90 days per call:

   ```sh
   curl -u admin:$ADMIN_PASSWORD -H 'Content-Type: application/json' \
     https://<host>.<tailnet>.ts.net:8443/admin/backfill \
     -d '{"summary_types": ["dailies", "sleeps", "hrv", "stressDetails", "activities"],
          "start": "2026-08-01", "end": "2026-10-24"}'
   ```

   The data arrives over the following minutes or hours via the webhook.
7. **Add to Claude.** In claude.ai → Settings → Connectors → Add custom connector, use
   `https://<host>.<tailnet>.ts.net/mcp`. Claude registers itself and opens `/mcp-login`.
   Check that it names Claude and a `claude.ai` callback, then enter the admin password.

## Operations

- **Daily jobs** (in-process, first run 5 minutes after start):
  - *Gap-check:* looks at the last `GAP_CHECK_DAYS` days (excluding the newest 2) for
    days missing `dailies`, `sleeps`, `hrv` or `stressDetails`, and requests a backfill
    for them. A day is retried weekly, at most 3 times.
  - *Backup:* writes a consistent copy to `backups/`, keeping the newest `BACKUP_KEEP`.
    Run one by hand with `docker compose exec garmin-reasoner python -m garmin_reasoner.backup`.
- **Off-site backups are up to you.** Copy `backups/` somewhere else, such as another machine
  or encrypted cloud storage. Treat backups as secret: they contain health data and Garmin
  refresh tokens. MCP tokens are only stored hashed.
- **If Garmin access lapses** (refresh token expired or revoked), pings and backfills log
  `NoAccessTokenError`. Run `/connect` again.

## Things to verify against the Garmin portal docs

These came from public material and couldn't be checked against the partner-only docs:

- OAuth authorize URL and token response fields (`config.py`, `oauth.py`).
- Webhook payload field names: `summaryId`, `callbackURL`, `calendarDate`,
  `startTimeInSeconds`, `startTimeOffsetInSeconds`.
- Which field, if any, carries an upload time (`summaries.upload_time` is left NULL).
- Backfill endpoint names and limits (`BACKFILL_TYPES` in `backfill.py`). Some differ from
  the webhook's summary keys, e.g. `respiration` vs `allDayRespiration`.
- Whether Garmin offers any way to authenticate webhook requests (currently: unknown
  userIds are dropped, and ping callbacks are only fetched from `https://apis.garmin.com`).
