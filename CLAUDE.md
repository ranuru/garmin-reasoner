# Garmin data pipeline

Personal service that receives my Garmin Connect data, stores it, and exposes it to Claude through a remote MCP server (added as a custom connector in claude.ai). Single user (me). Built myself as a learning/portfolio project — prefer clear, well-tested code over clever shortcuts.

## Architecture

```
Watch → Garmin Connect → (push/ping) → webhook service → SQLite
                                                           ↓
                                  /mcp endpoint ← Claude (custom connector)
```

One Python service (FastAPI), routes:

1. `GET /connect` → start OAuth 2.0 + PKCE flow with Garmin.
2. `GET /callback` → exchange code for tokens; store access + refresh token; fetch and store Garmin API user ID (`GET https://apis.garmin.com/wellness-api/rest/user/id`).
3. `POST /garmin/webhook` → receive push and ping notifications. For ping, call the `callbackURL` in each entry to fetch the data. Respond 200 fast; process in the background.
4. `/mcp` → remote MCP server (streamable HTTP) with read-only tools, e.g.:
   - `get_daily_summaries(start, end)`
   - `get_sleep(start, end)`
   - `get_hrv(start, end)`
   - `get_activities(start, end)`
   - `get_stress(start, end)`
5. `POST /admin/backfill` (auth-protected) → request historical backfill per summary type; results arrive asynchronously via the webhook.

## Hard constraints from Garmin's API

- **OAuth 2.0 + PKCE only.** OAuth 1.0a is retired end of 2026 — don't use any library or tutorial based on it.
  - Token URL: `https://diauth.garmin.com/di-oauth2-service/oauth/token` (verify against the docs in my developer portal; older PDFs show `connectapi.garmin.com`).
  - Refresh tokens before expiry; access token expiry is short.
- **Data is pushed to us**, not polled. Notifications carry the Garmin userId.
- **Query windows are based on upload time, not activity date, max 24h per request.** Chunk any pull/backfill by day.
- **Backfill is async** — it lands on the webhook like normal data.
- Must offer disconnect → `DELETE https://apis.garmin.com/wellness-api/rest/user/registration`.
- Full docs are partner-gated in the Garmin developer portal; check payload shapes there rather than guessing.

## Storage

SQLite. Store **raw JSON** per summary in a table like `summaries(summary_id PK, type, user_id, calendar_date, start_time, upload_time, raw_json, received_at)`. Upsert on summary_id (Garmin can resend). Parse fields at query time or in views — never lose raw data on schema changes.

## Security (this is health data on the public internet)

- MCP endpoint must have real auth (OAuth for the custom connector), not an obscure URL.
- Admin routes behind auth.
- Validate webhook requests as far as Garmin allows; reject unknown userIds.
- Secrets via env vars only (`GARMIN_CLIENT_ID`, `GARMIN_CLIENT_SECRET`, `REDIRECT_URI`, etc.). Never commit them; `.env` in `.gitignore`.

## Hosting

Runs on my home server (already on my Tailscale tailnet), in Docker.

- Public routes (`/callback`, `/garmin/webhook`, `/mcp`) exposed via **Tailscale Funnel** — Garmin and claude.ai call from the internet, so tailnet-only won't work for these.
- Admin routes (`/connect`, `/admin/*`) reachable **only via the tailnet** (`tailscale serve`), never through Funnel.
- Funnel only allows a few ports (443/8443/10000) and gives a `*.ts.net` HTTPS URL — that URL is what gets registered in the Garmin portal and as the OAuth redirect URI.
- Downtime: home power/internet outages can drop notifications. Add a gap-check job that detects missing days and requests a backfill for them.
- Back up the SQLite file (it's the only copy of the history).

## Build order

1. Project skeleton, config, SQLite schema, tests setup (pytest).
2. Webhook receiver + storage, tested against fake push/ping payloads.
3. OAuth PKCE flow + token refresh.
4. MCP server with read-only tools.
5. Backfill endpoint.
6. Deploy (Dockerfile + docker-compose on the home server, Tailscale Funnel/Serve config), register webhook URLs in the Garmin portal.
7. Gap-check + backfill job, SQLite backups.

## Open questions to check in the developer portal before step 3

- Which APIs/summary feeds are enabled for my app (Health, Activity)?
- Am I in the evaluation environment, and what are its limits?
- Push or ping for each feed?
