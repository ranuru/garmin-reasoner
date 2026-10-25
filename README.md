# garmin-reasoner

Personal service that pulls my Garmin Connect data through Garmin's
[Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api/),
stores it as raw JSON in SQLite, and exposes it to Claude through a remote MCP server.
See [CLAUDE.md](CLAUDE.md) for the architecture, and
[docs/garmin-personal-api.json](docs/garmin-personal-api.json) for Garmin's OpenAPI spec.

## Routes

| Route | Exposure | Purpose |
| --- | --- | --- |
| `/garmin/mcp` | Funnel (public) | MCP server (streamable HTTP), bearer-token protected |
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

## MCP tools

All tools are read-only and take an inclusive `(start, end)` range of local dates
(`YYYY-MM-DD`). There are two kinds:

- **Compact summaries** (`compact.py`) come first. Each one returns flat rows of compact
  JSON, one row per line, with units in the key names. Fields with no recorded value are
  `null`, and dates in the range with no data at all are listed in `missing`. For 90
  days of real data each response is 1–6k tokens, against 30–110k for the raw tools.
- **Raw Garmin JSON** (`get_sleep`, `get_daily_summaries`, `get_activities`, `get_hrv`,
  `get_stress`, `get_body_battery`, `get_weight`) is for drilling into a few days for a
  detail the summaries leave out, such as SpO2, splits or running dynamics.

`list_available_data` shows what's stored and when each feed last synced.

| Tool | Max range | One row per |
| --- | --- | --- |
| `get_period_stats(start, end, group_by="week")` | 366 days | ISO week, calendar month, or the whole range (`"none"`) |
| `get_sleep_summary(start, end)` | 93 days | night, dated by the morning it ends |
| `get_daily_overview(start, end)` | 93 days | day |
| `get_activity_list(start, end)` | 93 days | activity |
| raw `get_*` tools | 93 days | Garmin record |

Bedtime and wake time are the local wall-clock time at each end of the night, so a
night across a DST change gets the right time at both ends. Bedtime averages pivot at
noon, so 23:30 and 00:30 average to midnight. Today's row is usually incomplete.

The example outputs below use made-up values.

**`get_period_stats`**: averages and medians, counts, and totals per period. Periods at
the ends of the range are clipped (see `days`). `im_total` counts vigorous minutes
twice, as Garmin's weekly intensity goal does.

```json
{"group_by":"week","start":"2026-10-12","end":"2026-10-25","count":2,"rows":[
{"period":"2026-W42","start":"2026-10-12","end":"2026-10-18","days":7,"nights":7,"sleep_min_avg":431,"sleep_min_median":425,"sleep_score_avg":79,"sleep_score_median":82,"bedtime_avg":"01:12","bedtime_median":"00:57","resting_hr_avg":46,"resting_hr_median":46,"hrv_avg":64,"hrv_median":66,"steps_avg":7410,"steps_median":6980,"stress_avg":29,"stress_median":28,"nights_under_7h":3,"nights_under_6h":1,"activities":{"running":2,"hiking":1},"im_moderate":118,"im_vigorous":46,"im_total":210,"weight_first_kg":81.4,"weight_last_kg":81.0},
{"period":"2026-W43","start":"2026-10-19","end":"2026-10-25","days":7,"nights":6,"sleep_min_avg":448,"sleep_min_median":451,"sleep_score_avg":86,"sleep_score_median":88,"bedtime_avg":"00:41","bedtime_median":"00:35","resting_hr_avg":45,"resting_hr_median":45,"hrv_avg":70,"hrv_median":71,"steps_avg":6820,"steps_median":7105,"stress_avg":26,"stress_median":25,"nights_under_7h":1,"nights_under_6h":0,"activities":{"running":2,"bouldering":1},"im_moderate":95,"im_vigorous":31,"im_total":157,"weight_first_kg":null,"weight_last_kg":null}
]}
```

**`get_sleep_summary`**: `hrv` is the overnight average in ms, joined from the HRV
feed. A night with HRV but no sleep record still gets a row.

```json
{"start":"2026-10-23","end":"2026-10-25","count":2,"missing":["2026-10-24"],"rows":[
{"date":"2026-10-23","bedtime":"00:57","wake":"08:03","sleep_min":425,"deep_min":72,"light_min":250,"rem_min":103,"awake_min":12,"score":82,"avg_stress":14,"avg_hr":52,"avg_resp":14.1,"hrv":80,"nap_min":0},
{"date":"2026-10-25","bedtime":"23:30","wake":"07:00","sleep_min":440,"deep_min":81,"light_min":262,"rem_min":97,"awake_min":10,"score":85,"avg_stress":12,"avg_hr":50,"avg_resp":13.8,"hrv":null,"nap_min":20}
]}
```

**`get_daily_overview`**: intensity minutes, Body Battery (`bb_*`) and average stress
(0–100), plus that day's activities.

```json
{"start":"2026-10-22","end":"2026-10-23","count":2,"missing":[],"rows":[
{"date":"2026-10-22","steps":8802,"floors":7,"resting_hr":42,"im_moderate":49,"im_vigorous":5,"active_kcal":650,"bb_max":100,"bb_min":20,"stress":30,"activities":["hiking 95min","running 27min"]},
{"date":"2026-10-23","steps":5722,"floors":3,"resting_hr":44,"im_moderate":14,"im_vigorous":0,"active_kcal":258,"bb_max":95,"bb_min":24,"stress":25,"activities":[]}
]}
```

**`get_activity_list`**: runs get `pace_min_km`, and other activities with a distance get
`speed_kmh`. `z1_min`–`z5_min` are the minutes in each heart rate zone.

```json
{"start":"2026-10-21","end":"2026-10-22","count":2,"rows":[
{"start":"2026-10-21 10:11","type":"strength_training","name":"Strength","duration_min":41.0,"distance_km":null,"pace_min_km":null,"speed_kmh":null,"avg_hr":107,"max_hr":161,"elev_gain_m":null,"training_effect":"anaerobic_capacity","z1_min":16,"z2_min":8,"z3_min":2,"z4_min":0,"z5_min":0},
{"start":"2026-10-22 14:35","type":"running","name":"Evening Run","duration_min":27.0,"distance_km":4.07,"pace_min_km":"6:39","speed_kmh":null,"avg_hr":164,"max_hr":177,"elev_gain_m":24,"training_effect":"lactate_threshold","z1_min":0,"z2_min":1,"z3_min":5,"z4_min":21,"z5_min":0}
]}
```

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
4. **Expose via Tailscale.** `sh deploy/tailscale.sh`. This puts the app on Funnel
   port 443 under `/garmin`, with `/admin` on tailnet-only Serve (port 8443). Set
   `PUBLIC_BASE_URL=https://<host>.<tailnet>.ts.net/garmin`. Check with
   `tailscale funnel status`.

   The path prefix lets the app share the hostname with something that owns `/`, such as
   Home Assistant, and claude.ai only connects on port 443. OAuth discovery for a
   prefixed server lives at `/.well-known/oauth-authorization-server/garmin`, so it
   doesn't collide with the other app's `/.well-known/oauth-authorization-server`.
   Check from outside the tailnet:
   `curl https://<host>.<tailnet>.ts.net/.well-known/oauth-authorization-server/garmin`
   should show `"issuer": "https://<host>.<tailnet>.ts.net/garmin"`.
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
   `https://<host>.<tailnet>.ts.net/garmin/mcp`. Paste the printed Client ID and Secret under
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
