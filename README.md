# garmin-reasoner

**Talk to an AI assistant about your own Garmin data.** A small self-hosted service
that syncs your Garmin Connect history into SQLite and serves it as a remote
[MCP](https://modelcontextprotocol.io) server. You can connect it to
[Claude](https://claude.ai) or any other assistant that supports MCP connectors, and ask
questions like:

> *How has my sleep been the last three months, and does it change on days I run?*
>
> *Compare my resting heart rate and HRV this month with the same month last year.*
>
> *Which of my runs this autumn had the best pace at the lowest heart rate?*

It runs on a machine you own, keeps the full raw history, and only hands the model
read-only, privacy-filtered data.

> [!NOTE]
> This is an independent hobby project. It is not affiliated with or endorsed by Garmin
> or Anthropic.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [What the model can see](#what-the-model-can-see)
- [Security](#security)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## Features

- **Hourly sync** from Garmin's free
  [Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api/):
  daily summaries, sleep, HRV, stress, Body Battery, weight and activities. It catches up
  on its own after downtime and picks up late watch uploads.
- **Raw history kept forever.** Every record is stored as Garmin sent it, so new
  questions never need a re-download.
- **Token-efficient tools.** Compact summaries (one flat row per night, day,
  activity, week or month) come first, with raw JSON available for drill-down.
- **Private by default.** GPS coordinates and account or device identifiers never reach
  the model. Menstrual cycle and nutrition data are never synced.
- **Real authentication.** Its own OAuth server for MCP connectors, with every
  connection approved by you, and nothing else reachable from the internet.
- **Low maintenance.** One Docker container, SQLite, daily backups, and a warning before
  your API key expires.

## How it works

```
 Watch ──▶ Garmin Connect ◀── hourly pull (API key) ── garmin-reasoner ──▶ SQLite
                                                              │
     Claude / MCP client ── OAuth + MCP over HTTPS ──▶ /mcp  (via your reverse proxy)
```

A single Python (FastAPI) service does three things:

1. **Syncs** each feed from the Garmin Personal API on a schedule and upserts the raw
   JSON into SQLite.
2. **Serves an MCP server** at `/mcp` (streamable HTTP) with read-only tools over the
   stored data.
3. **Runs an OAuth authorization server** for MCP connectors. Approving a
   connection takes your admin password.

Assistants like claude.ai connect to the server from the internet, so the app needs a public HTTPS
URL. You can get one with any reverse proxy or tunnel. Admin tasks (history sync,
managing connections, backups) are commands you run on the machine, so the network only
ever sees the MCP and OAuth routes.

## Requirements

- A **Garmin Connect** account with a watch, and a **Personal API key** (free, created in
  Garmin Connect on the web).
- A machine with **Docker** and Docker Compose: a home server, a VPS, or your laptop.
  It syncs while it's running and catches up after downtime, but your assistant can
  only reach it while it's on, so an always-on machine works best.
- A way to give it a **public HTTPS URL**, such as
  [Tailscale Funnel](https://tailscale.com/kb/1223/funnel),
  [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
  or a reverse proxy like [Caddy](https://caddyserver.com) or nginx with your own domain.
- An **AI assistant that supports remote MCP servers** with OAuth, such as a Claude plan
  with custom connectors.

## Quick start

### 1. Get the code and a Garmin API key

```sh
git clone https://github.com/ranuru/garmin-reasoner.git
cd garmin-reasoner
cp .env.example .env
chmod 600 .env
```

In Garmin Connect on the web, go to **Settings → Personal API Key** and create a key. It
is shown only once. Put it in `.env` as `GARMIN_API_KEY`, and put the expiry date you
picked in `GARMIN_API_KEY_EXPIRES`.

### 2. Fill in the rest of `.env`

```sh
PUBLIC_BASE_URL=https://<your-host>/garmin
ADMIN_PASSWORD=<generate one: openssl rand -base64 24>
```

`PUBLIC_BASE_URL` is the address your assistant will use (see step 4). `ADMIN_PASSWORD` must
be at least 16 characters. It guards the public page where you approve an
assistant's access, so use a generated password. See
[Configuration](#configuration) for every option.

### 3. Start it

```sh
mkdir -p data backups && sudo chown 10001 data backups
docker compose up -d --build
docker compose logs -f
```

The first sync starts about 10 seconds after startup and pulls the last 90 days. Wait
for `sync finished` in the logs.

### 4. Make it reachable over HTTPS

The app listens on `127.0.0.1:8000`. Put an HTTPS reverse proxy or tunnel in front of
it that forwards these paths, with the original `Host` header:

- `/garmin` and everything under it
- `/.well-known/oauth-authorization-server/garmin`
- `/.well-known/oauth-protected-resource/garmin` and everything under it

The two `/.well-known/...` paths are where OAuth clients look for a server that lives
under a path ([RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) and
[RFC 9728](https://www.rfc-editor.org/rfc/rfc9728)). The prefix lets the app share a
hostname with other services. If you change it, change the path in `PUBLIC_BASE_URL`
to match.

It's safe to forward the whole prefix. The app only answers the MCP and OAuth routes
under it and returns 404 for everything else.

<details open>
<summary><b>Tailscale Funnel</b></summary>

```sh
sh deploy/tailscale.sh
```

This gives you `https://<host>.<tailnet>.ts.net/garmin`. Set `PREFIX=/other` to use a
different path.

</details>

<details>
<summary><b>Caddy</b> (your own domain, automatic HTTPS)</summary>

```caddy
garmin.example.com {
    @garmin path /garmin /garmin/* /.well-known/oauth-authorization-server/garmin /.well-known/oauth-protected-resource/garmin*
    handle @garmin {
        reverse_proxy 127.0.0.1:8000
    }
    respond 404
}
```

</details>

<details>
<summary><b>nginx</b></summary>

```nginx
location /garmin { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
location /.well-known/oauth-authorization-server/garmin { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
location /.well-known/oauth-protected-resource/garmin { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
```

</details>

Check it from a device on **another network** (e.g. your phone without Wi-Fi):

```sh
curl https://<your-host>/.well-known/oauth-authorization-server/garmin
```

The response should contain `"issuer": "https://<your-host>/garmin"`.

### 5. Connect your assistant

**Claude.** Create an OAuth client on the server:

```sh
docker compose exec garmin-reasoner python -m garmin_reasoner.mcp_clients create
```

In claude.ai, go to **Settings → Connectors → Add custom connector**:

- **URL:** `https://<your-host>/garmin/mcp`
- **Advanced settings:** paste the Client ID and Secret that were printed.

When you click Connect, an approval page opens. Check that it names Claude and a
`claude.ai` callback, then enter your admin password. Done. Try asking Claude
*"What Garmin data do you have access to?"*

**Other MCP clients.** Any client that supports remote MCP servers over streamable HTTP
with OAuth should work. For safety, only clients whose OAuth callback URL is on an
allowlist can connect, and the default list holds only Claude's. Find your client's
callback URL in its documentation and add it to `.env`, keeping Claude's if you use both:

```sh
MCP_ALLOWED_REDIRECT_URIS=["https://claude.ai/api/mcp/auth_callback","https://<your-client>/oauth/callback"]
```

Then run `docker compose up -d`, and add `https://<your-host>/garmin/mcp` as a connector
in your client. Clients that register themselves need nothing else. For clients that
ask for a Client ID and Secret, create them with the `mcp_clients create` command above.
Clients that use a `localhost` callback on a random port, as some desktop and editor
apps do, aren't supported yet.

### 6. Load older history (optional)

The first sync covers 90 days. To pull more, run the sync command on the machine:

```sh
docker compose exec garmin-reasoner python -m garmin_reasoner.sync 2025-01-01 2025-12-31
```

Add `--feeds sleeps hrv` to pull only some feeds. The command prints progress and
finishes when everything is stored. History goes back as far as your first watch.

## Configuration

All settings are environment variables, read from `.env`.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GARMIN_API_KEY` | yes | | Personal API key from Garmin Connect. |
| `GARMIN_API_KEY_EXPIRES` | | | Key expiry (`YYYY-MM-DD`). The log warns 14 days before it. |
| `PUBLIC_BASE_URL` | yes | | Public HTTPS URL including the path prefix, e.g. `https://garmin.example.com/garmin`. Used as the OAuth issuer. |
| `ADMIN_PASSWORD` | yes | | At least 16 characters. Approves an assistant's access on the connection approval page. |
| `SYNC_INTERVAL_MINUTES` | | `60` | How often to sync. |
| `SYNC_LOOKBACK_DAYS` | | `3` | Days before the last synced date that each sync re-pulls, to catch late uploads. |
| `BACKUP_KEEP` | | `14` | Number of daily backups to keep. |
| `JOBS_ENABLED` | | `true` | Turn the scheduled sync and backups off, e.g. for development. |
| `MCP_ALLOWED_REDIRECT_URIS` | | claude.ai and claude.com callbacks | JSON list of OAuth callback URLs. Only MCP clients with these exact callbacks may connect. |
| `DATABASE_PATH`, `BACKUP_DIR` | | set by Compose | Locations of the SQLite file and backups. |

`deploy/tailscale.sh` also takes `PREFIX` (default `/garmin`, must match the path of
`PUBLIC_BASE_URL`).

## What the model can see

All tools are read-only and take an inclusive date range (`start`, `end`) in your local
calendar dates (`YYYY-MM-DD`).

**Compact summaries (the model is told to use these first).** Flat rows with units in the key names,
`null` for values that weren't recorded, and dates with no data listed in `missing`. For
90 days of data, a response is 1–6k tokens, compared with 30–110k for the raw tools.

| Tool | Max range | One row per |
| --- | --- | --- |
| `get_period_stats(start, end, group_by)` | 366 days by `"week"`; unlimited by `"month"` or `"none"` | ISO week, calendar month, or the whole range |
| `get_sleep_summary` | 93 days | night, dated by the morning it ends |
| `get_daily_overview` | 93 days | day |
| `get_activity_list` | 93 days | activity |

**Raw Garmin JSON (for detail).** `get_daily_summaries`, `get_sleep`, `get_hrv`,
`get_stress`, `get_body_battery`, `get_weight` and `get_activities`, up to 93 days each.
The model uses these to drill into details the summaries leave out, such as SpO2, splits or
running dynamics.

**`list_available_data`** shows what's stored and when each feed last synced.

<details>
<summary>Example outputs (made-up values)</summary>

**`get_period_stats`**: averages, medians, counts and totals per period. The range is
clipped to the dates that have data, so monthly stats from `2000-01-01` cover all of
your history. `im_total` counts vigorous minutes twice, as Garmin's weekly intensity
goal does.

```json
{"group_by":"week","start":"2026-10-12","end":"2026-10-25","count":2,"rows":[
{"period":"2026-W42","start":"2026-10-12","end":"2026-10-18","days":7,"nights":7,"sleep_min_avg":431,"sleep_min_median":425,"sleep_score_avg":79,"sleep_score_median":82,"bedtime_avg":"01:12","bedtime_median":"00:57","resting_hr_avg":46,"resting_hr_median":46,"hrv_avg":64,"hrv_median":66,"steps_avg":7410,"steps_median":6980,"stress_avg":29,"stress_median":28,"nights_under_7h":3,"nights_under_6h":1,"activities":{"running":2,"hiking":1},"im_moderate":118,"im_vigorous":46,"im_total":210,"weight_first_kg":81.4,"weight_last_kg":81.0},
{"period":"2026-W43","start":"2026-10-19","end":"2026-10-25","days":7,"nights":6,"sleep_min_avg":448,"sleep_min_median":451,"sleep_score_avg":86,"sleep_score_median":88,"bedtime_avg":"00:41","bedtime_median":"00:35","resting_hr_avg":45,"resting_hr_median":45,"hrv_avg":70,"hrv_median":71,"steps_avg":6820,"steps_median":7105,"stress_avg":26,"stress_median":25,"nights_under_7h":1,"nights_under_6h":0,"activities":{"running":2,"bouldering":1},"im_moderate":95,"im_vigorous":31,"im_total":157,"weight_first_kg":null,"weight_last_kg":null}
]}
```

**`get_sleep_summary`**: `hrv` is the overnight average in ms. Bedtime and wake time
are local wall-clock times, correct across DST changes, and bedtime averages pivot at
noon, so 23:30 and 00:30 average to midnight.

```json
{"start":"2026-10-23","end":"2026-10-25","count":2,"missing":["2026-10-24"],"rows":[
{"date":"2026-10-23","bedtime":"00:57","wake":"08:03","sleep_min":425,"deep_min":72,"light_min":250,"rem_min":103,"awake_min":12,"score":82,"avg_stress":14,"avg_hr":52,"avg_resp":14.1,"hrv":80,"nap_min":0},
{"date":"2026-10-25","bedtime":"23:30","wake":"07:00","sleep_min":440,"deep_min":81,"light_min":262,"rem_min":97,"awake_min":10,"score":85,"avg_stress":12,"avg_hr":50,"avg_resp":13.8,"hrv":null,"nap_min":20}
]}
```

**`get_daily_overview`**: steps, intensity minutes, Body Battery (`bb_*`), average
stress (0–100) and that day's activities.

```json
{"start":"2026-10-22","end":"2026-10-23","count":2,"missing":[],"rows":[
{"date":"2026-10-22","steps":8802,"floors":7,"resting_hr":42,"im_moderate":49,"im_vigorous":5,"active_kcal":650,"bb_max":100,"bb_min":20,"stress":30,"activities":["hiking 95min","running 27min"]},
{"date":"2026-10-23","steps":5722,"floors":3,"resting_hr":44,"im_moderate":14,"im_vigorous":0,"active_kcal":258,"bb_max":95,"bb_min":24,"stress":25,"activities":[]}
]}
```

**`get_activity_list`**: runs get `pace_min_km`, and other activities with a distance get
`speed_kmh`. `z1_min`–`z5_min` are minutes in each heart rate zone.

```json
{"start":"2026-10-21","end":"2026-10-22","count":2,"rows":[
{"start":"2026-10-21 10:11","type":"strength_training","name":"Strength","duration_min":41.0,"distance_km":null,"pace_min_km":null,"speed_kmh":null,"avg_hr":107,"max_hr":161,"elev_gain_m":null,"training_effect":"anaerobic_capacity","z1_min":16,"z2_min":8,"z3_min":2,"z4_min":0,"z5_min":0},
{"start":"2026-10-22 14:35","type":"running","name":"Evening Run","duration_min":27.0,"distance_km":4.07,"pace_min_km":"6:39","speed_kmh":null,"avg_hr":164,"max_hr":177,"elev_gain_m":24,"training_effect":"lactate_threshold","z1_min":0,"z2_min":1,"z3_min":5,"z4_min":21,"z5_min":0}
]}
```

</details>

<details>
<summary>Synced feeds and Garmin endpoints</summary>

| Feed | Garmin endpoint | Max range per request |
| --- | --- | --- |
| `dailies` | `usersummary-service/v2/dailysummaries` | 365 days |
| `sleeps` | `sleep-service/sleep/dailySleepSummariesByDate` | 31 days |
| `hrv` | `hrv-service/hrv/daily/{from}/{to}` | 28 days (undocumented) |
| `stress` | `usersummary-service/stats/stress/daily/{start}/{end}` | 28 days |
| `bodyBattery` | `wellness-service/wellness/bodyBattery/reports/daily` | 31 days |
| `weight` | `weight-service/weight/range/{start}/{end}?includeAll=true` | 31 days (undocumented) |
| `activities` | `activitylist-service/activities/search/activities` | 31 days, paged |

Menstrual cycle and nutrition endpoints are deliberately not synced.

</details>

## Security

This is health data on the public internet, so the defaults are strict:

- **Only the MCP and OAuth routes are public.** Under its prefix, the app answers 404
  for anything other than `/mcp` and the OAuth flow. There is no admin API: admin tasks
  are commands you run on the machine. The health check is only for the container.
- **Every connection is approved by you** with the admin password, and only clients with an
  allowed callback URL (`MCP_ALLOWED_REDIRECT_URIS`) may register. Failed logins are throttled.
- **Tokens and codes are stored hashed**, refresh tokens rotate on use, and access tokens
  last an hour.
- **The model never sees** GPS coordinates or account and device identifiers (`HIDDEN_FIELDS`
  in [`queries.py`](src/garmin_reasoner/queries.py)). They stay only in the raw store.
- **Abuse limits:** capped client registrations, pending authorizations and request
  sizes, plus strict security headers.
- **Locked-down container:** it runs as a non-root user on a read-only filesystem, with
  all capabilities dropped and only loopback port binding. The database and backups are
  readable by their owner only.
- **Secrets come only from environment variables**, and they are never logged or stored
  in the database.

Found a security problem? Please report it privately through
[GitHub's security advisories](https://github.com/ranuru/garmin-reasoner/security/advisories/new)
rather than in a public issue.

## Operations

- **Sync** runs every `SYNC_INTERVAL_MINUTES`. Each run re-pulls a few days before the
  last synced date, so late uploads and outages (up to 90 days) heal on their own. Ask
  your assistant to run `list_available_data` to see the last sync time and any error per feed.
- **Rotating the API key:** keys last up to a year. Create a new one, update `.env`, and
  run `docker compose up -d`.
- **Backups:** a consistent copy goes to `backups/` every day. Run one by hand with
  `docker compose exec garmin-reasoner python -m garmin_reasoner.backup`. Copy backups
  somewhere off the machine, because they are the only copy of your history. They're
  owned by the container user, so copy them with `sudo`.
- **Managing connections:** `python -m garmin_reasoner.mcp_clients list` shows clients,
  and `delete <id>` removes one and revokes its tokens. Run both with
  `docker compose exec garmin-reasoner`.
- **Updating:** `git pull && docker compose up -d --build`. Database migrations run
  automatically on startup.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `cannot write to /app/data (running as uid 10001)` at startup | `sudo chown -R 10001 data backups` |
| `API key exchange failed: HTTP 401` in the logs | The key is wrong or expired. Create a new one. |
| The assistant can't connect | Run the discovery `curl` from step 4 from another network. `PUBLIC_BASE_URL` must match the public URL exactly, including `/garmin`, and the proxy must pass the original `Host` header. |
| The approval page says the request is unknown or expired | Approval requests last 10 minutes. Start the connection again from your assistant. |
| `Too many attempts; try later.` | Ten failed passwords lock the page for 15 minutes. |

<details>
<summary>Garmin API quirks</summary>

Learned against the live API. Garmin's spec isn't always right.

- The key exchange returns the access token as a bare JWT (`text/plain`), valid 24h by
  its `exp` claim, and it's sent as `Authorization: Bearer <token>`.
- `weight/all` answers HTTP 400 for valid ranges, so weight uses
  `weight/range?includeAll=true`. Weight values are in **grams**, not kilograms.
- Body Battery returns an empty report for every date asked for, including days before
  your first watch. The sync skips those.
- Date-range limits for HRV, weight and activities, and rate limits, are undocumented.
  The client uses conservative chunks and retries 429 and 5xx responses with backoff,
  honouring `Retry-After`.

</details>

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                 # install dependencies
uv run pytest           # run the tests
uv run ruff check .     # lint
uv run uvicorn garmin_reasoner.main:create_app --factory --reload
```

For local development, `PUBLIC_BASE_URL=https://localhost` works until a proxy is set
up. Adding a Garmin feed takes one fetch function and one entry in `FEEDS` in
[`sync.py`](src/garmin_reasoner/sync.py).

This is primarily a personal project, but issues and pull requests are welcome. Please
include tests with code changes.

## License

[MIT](LICENSE). Garmin and Garmin Connect are trademarks of Garmin Ltd. Claude is a
trademark of Anthropic.
