# Deploying Agent Town on free tiers

```
Browser ──▶ Vercel (static React app, web/)
   │            └─ /traces/*.json: the recorded presets (work with no backend)
   └──▶ Render free web service (Docker): ONE process = API + dispatcher + event publisher + workers
                    ├──▶ Neon free Postgres   (jobs, outbox, events)
                    └──▶ Upstash free Redis   (streams, delayed set, heartbeats, limits)
```

Cost: nothing. No paid service is used, and none is required. Free tiers change, so check each
provider's current limits before you rely on them (the ones this project designs around are noted
below).

## Why one process
A free host gives you one service. `LIVE_DEMO=true` therefore runs the workers inside the API
process (`EMBEDDED_WORKERS`), which is also what lets the chaos buttons kill and start a worker. It
is also why the Dockerfile runs exactly one uvicorn worker: more than one process would each start
their own workers and the chaos controls would only reach one of them.

## Environment variables

Set these on **Render** (dashboard, or `render.yaml` + Blueprint). Secrets are `sync: false` in
`render.yaml`, so Render asks for them and they are never in git.

| Variable | Value | Secret? | Why |
|---|---|---|---|
| `LIVE_DEMO` | `true` | no | Embedded workers, 2 quiet workers, caps, flood limit 100, sleep when idle |
| `AUTO_MIGRATE` | `true` | no | Neon starts empty; the app applies `migrations/*.sql` at startup |
| `TRUST_FORWARDED_FOR` | `true` | no | Render's proxy sets `X-Forwarded-For`; without it every visitor shares one per-IP limit |
| `PLANNER` | `fixed` | no | Presets only: a built-in task graph, no LLM. Free-text goals get a 400 and the web app hides the box. Use `gemini` only if you want the real planner |
| `DATABASE_URL` | Neon **direct** string ending `?sslmode=require` | **yes** | See "Neon" |
| `REDIS_URL` | Upstash `rediss://default:PASSWORD@HOST:6379` | **yes** | `rediss` = TLS |
| `CORS_ORIGINS` | `https://YOUR-SITE.vercel.app` (no trailing slash) | no | Only your site may call the API or open the WebSocket |
| `GEMINI_API_KEY` | leave empty with `PLANNER=fixed` | **yes** | Not needed (and ignored) when `PLANNER=fixed`. With `PLANNER=gemini`: empty = mock planner only. Server-side only, never in the web app |
| `GEMINI_DAILY_CAP` | `50` | no | Hard daily cap on real Gemini calls; over it, the mock planner runs |
| `LIVE_GOALS_DAILY_CAP` | `30` | no | Live goals per UTC day across everyone; over it the API answers 429 |

On **Vercel** (Project Settings, Environment Variables, build time):

| Variable | Value |
|---|---|
| `VITE_API_URL` | `https://YOUR-SERVICE.onrender.com` (no trailing slash) |

That value is public (it ends up in the JavaScript), which is fine: it is a URL, not a secret.

The backend refuses to start with `LIVE_DEMO=true` if `CORS_ORIGINS` is empty, `*`, not `https://`,
points at localhost, has a trailing slash, or if `TRUST_FORWARDED_FOR` is off. A bad deploy fails
in the logs instead of running open.

## Steps (in this order)

1. **Neon**: create a project. Copy the **direct** connection string (the host WITHOUT `-pooler`),
   make sure it ends with `?sslmode=require`.
2. **Upstash**: create a Redis database (any region near Render). Copy the `rediss://` URL from
   the dashboard (Redis connect tab, "TLS").
3. **Render**: New, Blueprint, connect the GitHub repo (it reads `render.yaml`), or New, Web Service,
   Docker, free plan, health check path `/healthz`. Fill in the variables above (leave
   `CORS_ORIGINS` as a placeholder for now). Deploy. Note the service URL.
4. **Vercel**: Import the repo, set **Root Directory** to `web`, add `VITE_API_URL` = the Render
   URL, deploy. Note the site URL.
5. **Render again**: set `CORS_ORIGINS` to the Vercel URL, save (Render redeploys).
6. **Check** (below).

## Check it works
```bash
curl https://YOUR-SERVICE.onrender.com/healthz
# {"status":"ok","live":true,"sleeping":false}   (the first call may take ~1 minute: cold start)

curl -s -o /dev/null -w "%{http_code}\n" -X OPTIONS https://YOUR-SERVICE.onrender.com/goals \
  -H "Origin: https://evil.example" -H "Access-Control-Request-Method: POST"
# 400: another site is refused
```
Then open the Vercel site. You should see "Recorded Trace (Waking Live Backend...)" with a preset
already playing, then "Live Backend Connected" and the page switching to live. Type a goal, watch
workers move, approve the card, press Kill worker.

## What the free tiers do to the demo
- **Render free** spins the service down after about 15 minutes without traffic and takes up to a
  minute to start again. The site starts on a recorded trace and pings `/healthz` for 60 seconds
  (every 5s), then every 15s in the background: this is exactly why the recorded mode exists.
- **Neon free** suspends compute after a few idle minutes. `LIVE_DEMO` makes the app go to sleep
  itself after `IDLE_SLEEP_SECONDS` (300) with no new goals, which stops polling so Neon can
  suspend too. The pool drops idle connections after 60 seconds so it reconnects instead of using a
  dead one.
- **Upstash free** has a monthly command limit (500K is what the code assumes, `REDIS_MONTHLY_LIMIT`).
  Quiet polling (workers block for 5 seconds, 10s heartbeats) plus a guard that turns live mode off
  at 90% keep the demo inside it. `/healthz` reports `live:false` when the guard has closed live mode;
  the site then stays on recordings.
- **Gemini** is capped per day (`GEMINI_DAILY_CAP`) and falls back to the mock planner. The chaos
  flood button only ever creates mock jobs.

## Neon: direct or pooled?
Use the **direct** host. Neon's `-pooler` host is PgBouncer in transaction mode, which cannot keep
prepared statements, and asyncpg uses them ("prepared statement does not exist"). We open at most
10 connections, so we do not need a pooler. If you must use the pooled host, the app notices
`-pooler` in the name and sets `statement_cache_size=0` itself (`app/db.py`); for another pooler,
set `DB_POOLER=true`.

## Migrations
`AUTO_MIGRATE=true` applies `migrations/*.sql` once each at startup (`app/migrate.py`, history in
`schema_migrations`, one transaction per file, under an advisory lock). To run them from your
laptop instead:
```bash
DATABASE_URL='postgresql://USER:PASSWORD@HOST/DB?sslmode=require' python -m app.migrate
```
It refuses a database that already has tables but no history (it will not guess which files ran).

## Secrets and rotation
- Nothing secret is committed: `python scripts/secret_scan.py` runs in CI and fails on API keys,
  private keys, tokens, a tracked `.env`, or a database/Redis URL with a real password. It only
  reads the current files, not git history: if a secret was ever committed, rotate it.
- The Gemini key lives only in Render's environment. The browser never sees it.
- To rotate: change the value in Neon/Upstash/Google, then update it in Render.

## Pinned dependencies
`requirements.lock` pins every package version the image installs. A newer `redis-py` once changed
a default timeout and broke the workers in the live profile, found only by booting a clean
environment, so deploys use the tested versions. To update them:
```bash
python3.12 -m venv /tmp/v && /tmp/v/bin/pip install -r <(python -c "import tomllib;print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))") \
  && /tmp/v/bin/pip freeze > requirements.lock
```
then run the tests and boot the app before committing.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| Site says "Live Backend Offline" for minutes | Render cold start or crash: check the Render logs. A startup error from the config check names the variable |
| Browser console: CORS error | `CORS_ORIGINS` does not match the site's exact origin (scheme, no trailing slash) |
| WebSocket closes immediately | Same origin problem (the WebSocket origin is checked against `CORS_ORIGINS`) |
| `prepared statement ... does not exist` | `DATABASE_URL` is the pooled host and something bypassed `app/db.py` |
| `SSL` / `connection refused` to Postgres | Missing `?sslmode=require` |
| 429 on goals | The daily live-goal cap or the per-IP limit: the page shows a friendly message and the presets still work |
| `/healthz` says `live:false` | The Redis command-budget guard closed live mode for the month |

## Not tested here
The Docker image, Render, Neon, Upstash and Vercel could not be exercised from the development
machine (no Docker, no accounts). What was verified: the app boots from a clean virtualenv built
from the same dependency list with the live-demo variables set, applies the migrations to an empty
database, runs a goal to success with approval, has no worker errors, and answers a foreign-origin
request with no CORS headers; the TLS and hosted-service paths follow the providers' documentation.
Treat the first real deploy as the real test and read the Render logs.
