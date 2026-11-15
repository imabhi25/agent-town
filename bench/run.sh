#!/usr/bin/env bash
# One benchmark run against the LOCAL stack (Postgres + Redis on this machine, API with embedded
# workers on port 8000). It WIPES the job tables and Redis DB in DATABASE_URL / REDIS_URL, so
# point those at scratch services.
#
#   bench/run.sh rate  <workers> <req_per_sec> <duration>     e.g. bench/run.sh rate 4 300 20s
#   bench/run.sh burst <workers> <total_jobs>                 e.g. bench/run.sh burst 8 2000
#
# Needs: k6, psql, python with the project installed. Prints one JSON line.
set -euo pipefail
MODE=$1; WORKERS=$2; A=$3; B=${4:-20s}
: "${DATABASE_URL:?set DATABASE_URL}" "${REDIS_URL:?set REDIS_URL}"
PY=${PY:-python}
cd "$(dirname "$0")/.."

$PY - <<PYEOF
import redis, os
redis.from_url(os.environ["REDIS_URL"]).flushdb()
PYEOF
psql "$DATABASE_URL" -qc "TRUNCATE goals, tasks, jobs, job_outputs, outbox, events CASCADE"

EMBEDDED_WORKERS=true WORKER_COUNT=$WORKERS $PY -m uvicorn app.main:app --port 8000 --log-level warning &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT
for _ in $(seq 60); do curl -sf localhost:8000/healthz >/dev/null && break; sleep 0.5; done

OUT=$(mktemp)
if [ "$MODE" = burst ]; then
  MODE=burst TOTAL=$A OUT=$OUT k6 run -q bench/jobs.js >/dev/null
else
  MODE=rate RATE=$A DURATION=$B OUT=$OUT k6 run -q bench/jobs.js >/dev/null
fi

# Wait for the workers to drain the queue.
for _ in $(seq 600); do
  left=$(psql "$DATABASE_URL" -Atc "SELECT count(*) FROM jobs WHERE status NOT IN ('succeeded','dead')")
  [ "$left" = 0 ] && break; sleep 0.5
done

ENGINE=$(psql "$DATABASE_URL" -At -F, -c "
  SELECT count(*) FILTER (WHERE status='succeeded'), count(*) FILTER (WHERE status='dead'),
         round(extract(epoch FROM max(completed_at)-min(created_at))::numeric,2),
         round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at-created_at))*1000)::numeric,0),
         round((percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at-created_at))*1000)::numeric,0),
         round((percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at-created_at))*1000)::numeric,0),
         (SELECT count(*) FROM job_outputs), (SELECT count(*) FROM jobs),
         (SELECT count(*) FROM events WHERE type='job.succeeded')
  FROM jobs")
$PY - "$MODE" "$WORKERS" "$ENGINE" "$OUT" <<'PYEOF'
import json, sys
mode, workers, engine, out = sys.argv[1:5]
ok, dead, secs, p50, p95, p99, outputs, jobs, succ_events = engine.split(",")
api = json.load(open(out))
secs = float(secs)
print(json.dumps({
  "mode": mode, "workers": int(workers), "jobs": int(jobs), "succeeded": int(ok), "dead": int(dead),
  "api": {k: api[k] for k in ("req_per_sec", "p50_ms", "p95_ms", "p99_ms", "failed_rate", "dropped_iterations")},
  "first_create_to_last_done_s": secs, "engine_jobs_per_sec": round(int(ok) / secs, 1) if secs else None,
  "e2e_ms": {"p50": int(p50), "p95": int(p95), "p99": int(p99)},
  "one_output_per_job": int(outputs) == int(jobs), "one_success_event_per_job": int(succ_events) == int(ok),
}))
PYEOF
