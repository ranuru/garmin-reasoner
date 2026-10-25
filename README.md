# garmin-reasoner

Personal service that pulls my Garmin Connect data through Garmin's
[Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api/),
stores it as raw JSON in SQLite, and exposes it to Claude through a remote MCP server.
See [CLAUDE.md](CLAUDE.md) for the architecture, and
[docs/garmin-personal-api.json](docs/garmin-personal-api.json) for Garmin's OpenAPI spec.

## Routes

| Route | Exposure | Purpose |
| --- | --- | --- |
| `/mcp` | Funnel (public) | MCP server (streamable HTTP), bearer-token protected |
| `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/*`, `/mcp-login` | Funnel | OAuth server for the claude.ai connector |
| `POST /admin/sync` | tailnet only | Pull a historical range, e.g. `{"start": "2025-10-01", "end": "2026-10-24"}` (optional `"feeds": [...]`) |
| `GET /healthz` | container only | Health check |

## Data synced

| Feed | Garmin endpoint | Max range per request |
| --- | --- | --- |
| `dailies` | `usersummary-service/v2/dailysummaries` | 365 days |
| `sleeps` | `sleep-service/sleep/dailySleepSummariesByDate` | 31 days |
| `hrv` | `hrv-service/hrv/daily/{from}/{to}` | 28 days (undocumented) |
| `stress` | `usersummary-service/stats/stress/daily/{start}/{end}` | 28 days |
| `bodyBattery` | `wellness-service/wellness/bodyBattery/reports/daily` | 31 days |
| `weight` | `weight-service/weight/range/{start}/{end}?includeAll=true` | 31 days (undocumented) |
| `activities` | `activitylist-service/activities/search/activities` | 31 days, paged |

The menstrual cycle and nutrition endpoints are deliberately not synced. Adding a feed
takes one fetch function and one entry in `FEEDS` in `sync.py`.

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

1. **Create an API key.** In Garmin Connect on the web, go to Settings → Personal API Key.
   Copy it into `.env` as `GARMIN_API_KEY` (it's shown only once), and put the expiry date
   you chose in `GARMIN_API_KEY_EXPIRES`.
2. **Configure the rest.** `cp .env.example .env` and fill it in. `PUBLIC_BASE_URL` is the
   server's Funnel URL (`https://<host>.<tailnet>.ts.net`). `ADMIN_PASSWORD` must be at least
   16 characters. It guards the public `/mcp-login` page, so use a generated one.
3. **Run.**

   ```sh
   mkdir -p data backups && sudo chown 10001 data backups
   docker compose up -d --build
   docker compose logs -f
   ```

   The first sync starts 10 seconds after startup and pulls the last 90 days. Look for
   `sync finished` in the logs.
4. **Expose via Tailscale.** `sh deploy/tailscale.sh`. This puts the MCP paths on Funnel
   (port 443) and `/admin` on tailnet-only Serve (port 8443). Check with
   `tailscale funnel status`.
5. **Load older history** (optional; up to 366 days per call):

   ```sh
   curl -u admin:$ADMIN_PASSWORD -H 'Content-Type: application/json' \
     https://<host>.<tailnet>.ts.net:8443/admin/sync \
     -d '{"start": "2025-07-01", "end": "2026-07-27"}'
   ```

6. **Add to Claude.** Create an OAuth client on the server:

   ```sh
   docker compose exec garmin-reasoner python -m garmin_reasoner.mcp_clients create
   ```

   In claude.ai → Settings → Connectors → Add custom connector, use the URL
   `https://<host>.<tailnet>.ts.net/mcp`. Paste the printed Client ID and Secret under
   **Advanced settings**. When you connect, `/mcp-login` opens: check that it names Claude
   and a `claude.ai` callback, then enter the admin password. (`... mcp_clients list` /
   `delete <id>` manage clients; deleting one revokes its tokens.)

## Operations

- **Sync** runs every `SYNC_INTERVAL_MINUTES` (default 60). For each feed it re-pulls from
  `SYNC_LOOKBACK_DAYS` days before the last synced date up to tomorrow. That picks up late
  watch uploads, and after an outage it catches up on its own (up to 90 days back).
  Claude's `list_available_data` tool shows the last sync time and error for each feed.
- **Rotating the API key:** keys last up to a year. The log warns 14 days before
  `GARMIN_API_KEY_EXPIRES`. Create a new key, update `.env`, and run
  `docker compose up -d`.
- **Backups:** every day the app writes a consistent copy to `backups/`, keeping the newest
  `BACKUP_KEEP`. Run one by hand with
  `docker compose exec garmin-reasoner python -m garmin_reasoner.backup`. Copy `backups/`
  somewhere else, such as another machine or encrypted cloud storage. Backups contain
  health data. MCP tokens are only stored hashed, and the Garmin key is never in the
  database.
- **Privacy:** activities' GPS start/end coordinates and account identifiers are kept in
  the raw data but never returned to Claude (`HIDDEN_FIELDS` in `queries.py`).

## Verified against the live API (2026-10-25)

- The key exchange returns the access token as a bare JWT (`text/plain`), valid 24h by
  its `exp` claim. It is sent as `Authorization: Bearer <token>`.
- Every feed works. `weight/all` answers HTTP 400 (with an empty body) for valid ranges,
  so weight uses `weight/range?includeAll=true` instead.
- Weight values come in **grams**, despite the spec saying kilograms.

Still undocumented: date-range limits for HRV, weight and activities (the client uses
conservative chunks), and rate limits (429 and 5xx responses are retried with backoff,
honouring `Retry-After`).
