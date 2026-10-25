# Garmin data pipeline

Personal service that pulls my Garmin Connect data, stores it, and exposes it to Claude through a remote MCP server (added as a custom connector in claude.ai). Single user (me). Built myself as a learning/portfolio project — prefer clear, well-tested code over clever shortcuts.

## Architecture

```
Watch → Garmin Connect ← (hourly pull, API key) ← sync job → SQLite
                                                               ↓
                                      /mcp endpoint ← Claude (custom connector)
```

One Python service (FastAPI):

1. **Sync job** (in-process, every `SYNC_INTERVAL_MINUTES`) → pulls each feed from the Garmin Personal API and upserts raw JSON. Re-pulls from a few days before the last synced date, which covers late uploads and heals downtime.
2. `/mcp` → remote MCP server (streamable HTTP) with read-only tools: `list_available_data`; compact summaries (`compact.py`, flat rows, use first) `get_period_stats`, `get_sleep_summary`, `get_daily_overview`, `get_activity_list`; raw Garmin JSON for drill-down `get_daily_summaries`, `get_sleep`, `get_hrv`, `get_activities`, `get_stress`, `get_body_battery`, `get_weight` (all `(start, end)`).
3. MCP OAuth endpoints (`/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/*`, `/mcp-login`) → our own authorization server for the claude.ai connector.
4. `POST /admin/sync` (auth-protected) → pull a historical range.

## Garmin Personal API

Free personal tier (launched October 2026), not the partner/business program. The spec is in `docs/garmin-personal-api.json` — check shapes and limits there rather than guessing.

- **API key, not OAuth.** Created in Garmin Connect web → Settings → Personal API Key; lasts 30 days to 1 year, shown once.
  - Exchange: `POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token` with `Authorization: ApiKey <key>` → the access token as a bare JWT (`text/plain`), valid 24h per its `exp` claim; send it as `Authorization: Bearer <token>`. No refresh tokens; exchange the key again.
- **The spec isn't always right:** `weight/all` returns 400 (use `weight/range?includeAll=true`), and weights are in grams, not kg. Verify against the live API when adding a feed.
- **Pull only, read only.** No webhooks/push/ping. Base URL `https://personal.connectapi.garmin.com`.
- **Endpoints take local calendar dates** (`YYYY-MM-DD`), with per-endpoint max ranges (e.g. sleep 31 days, stress 28, daily summaries 1 year). Chunk to fit.
- Rate limits aren't documented: retry 429/5xx with backoff, honour `Retry-After`.
- Don't sync menstrual cycle or nutrition data unless I ask.

## Storage

SQLite. Store **raw JSON** per record in `summaries(type, summary_id, user_id, calendar_date, start_time, upload_time, raw_json, received_at)`, primary key `(type, summary_id)`. `summary_id` is the record's natural key (`calendarDate` for daily feeds, `activityId`, weigh-in `samplePk`). Upsert (re-pulls are normal). Parse fields at query time or in views — never lose raw data on schema changes. Never edit an applied migration; add a new one.

## Security (this is health data on the public internet)

- MCP endpoint must have real auth (OAuth for the custom connector), not an obscure URL.
- Admin routes behind auth.
- Don't hand Claude GPS coordinates or account identifiers (`HIDDEN_FIELDS` in `queries.py`).
- Secrets via env vars only (`GARMIN_API_KEY`, `ADMIN_PASSWORD`, etc.). Never commit them, never log them; `.env` in `.gitignore`.

## Hosting

Runs on my home server (already on my Tailscale tailnet), in Docker.

- Only the MCP and MCP-OAuth routes are public, via **Tailscale Funnel** (claude.ai calls from the internet). Garmin never calls in.
- Admin routes (`/admin/*`) reachable **only via the tailnet** (`tailscale serve`), never through Funnel.
- Funnel only allows a few ports (443/8443/10000) and gives a `*.ts.net` HTTPS URL — that's `PUBLIC_BASE_URL`, the MCP OAuth issuer.
- Home Assistant owns `/` on Funnel port 443 of the same host and serves its own `/.well-known/oauth-authorization-server`. claude.ai only connects on 443, so this app lives under a path prefix: `PUBLIC_BASE_URL=https://<host>.ts.net/garmin`, Funnel routes `/garmin` plus `/.well-known/oauth-authorization-server/garmin` and `/.well-known/oauth-protected-resource/garmin` (RFC 8414/9728 path-insertion discovery). Never route an unprefixed OAuth path to this app, or to HA from this app's flow. `PathPrefix` in `mcp_auth.py` strips the prefix; any URL the app hands out must be absolute (built from `PUBLIC_BASE_URL`).
- Back up the SQLite file (it's the only copy of the history).

## Build status

Done: skeleton/config/schema, MCP server + OAuth, Personal API client + sync, admin sync, Docker + Tailscale config, backups. Superseded: the partner-API webhook receiver, Garmin OAuth PKCE flow, and push backfill (removed when switching to the Personal API; see git history).
