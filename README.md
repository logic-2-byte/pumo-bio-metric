# Biometric capture service

Talks to eSSL/ZKTeco fingerprint readers on a branch LAN and writes their
punches into the LMS database, where the LMS turns them into attendance.

```
reader ──(pyzk, TCP 4370)──┐
                           ├──▶ this service ──(INSERT)──▶ LMS  biometric_punch_log
reader ──(ADMS/iClock push)┘                                          │
                                                                      ▼
                                                   LMS reconciler ──▶ staff_punches
```

## The two things worth understanding before changing anything

**It writes to the LMS database directly, not to the LMS API.** A reader on a
branch LAN has to keep capturing while the backend is redeploying or
unreachable, and the cheapest way to guarantee that is to make capture depend
on nothing but Postgres. The LMS reconciles the raw rows on its own schedule.

**Recovery is not clever, and that is the point.** When a reader is
unreachable it keeps recording into its own flash memory — it holds thousands
of punches — so nothing is lost at the reader, only our copy. On every
reconnect, and every 15 minutes while connected, this service re-reads the
reader's *entire* memory and offers all of it. The unique constraint

```sql
UNIQUE (device_serial, device_user_id, device_time)
```

drops whatever is already stored and keeps whatever was missed. One mechanism
covers every failure: reader down, database down, or this process killed
mid-batch. It needs no cursor and no high-water mark — both are state that can
be wrong and neither survives a crash.

After each sweep it counts what the reader holds against what the database
stored and logs `in sync` or a loud `MISMATCH — N missing`.

## Layout

```
app/
  main.py           FastAPI app: dashboard + ADMS listener
  core/
    manager.py      startup/shutdown — starts the bridge
    logger.py       loguru sinks (call setup_logger exactly once)
    settings.py
  router/
    base.py         /api/pulse (liveness), /api/health (readiness)
    iclock.py       ADMS/iClock endpoints + the local dashboard
  sync/             ── the bridge ──
    config.py       LMS_DB_* and the timings
    models.py       Punch — the shape biometric_punch_log stores
    lms_db.py       the only 3 statements run against the LMS
    spool.py        disk fallback when Postgres is unreachable
    device_worker.py one thread per reader: connect, sweep, verify
    supervisor.py   discovers readers from the LMS, manages workers
    run.py          `python -m app.sync.run` — bridge with no web server
tools/              one-off device diagnostics, not part of the service
tests/
```

## Running it

```bash
cp .env.example .env      # fill in LMS_DB_*
docker compose up -d      # or: python -m app.sync.run
```

`python -m app.sync.run` is the better deployment for a branch box that has no
reason to serve a dashboard: no web server, no open port, just the sync.

## Where it can run — not a free choice

- **Polling** needs this service to reach readers at their LAN addresses. Works
  on a branch box; does **not** work from a cloud VM without a VPN.
- **ADMS push** needs the readers to reach this service.

Deployed to a cloud VM with no VPN, the polling half is inert and only pushed
punches arrive — which also loses the reader-memory sweep that recovers
outages. `.github/workflows/deploy.yml` currently deploys to a VM; if the
readers are on branch LANs, run the bridge at the branch instead.

## Registering a reader

Readers are registered in the **LMS console**, under Attendance → Biometric
Readers, not in a config file here. This service reads the list from
`biometric_devices` every two minutes, so a reader added there is polled
shortly after with no restart.

**There is no device configuration in `.env`, and that is deliberate.** A
company runs one or two readers per branch, so the fleet is a property of the
company, not of whichever host happens to run this process. An empty
`biometric_devices` means nothing is polled — correct, because a reader nobody
registered is a reader nobody has given us the serial of.

The one field that must match exactly is the serial. A push-protocol reader
sends its factory serial; the direct socket cannot ask for one and sends
`IP:192.168.0.210` instead. Whatever arrives is what goes in the console.

If the LMS is unreachable, the supervisor keeps the workers it already has
rather than treating an empty answer as "every reader was unregistered" — an
outage says nothing about which readers exist.

## Operational notes

- **One process per site.** `gunicorn_config.py` pins `workers = 1` — these
  readers accept a single TCP connection, so a second process fights the first
  for it. Read the comment there before changing it.
- **The spool must be on a volume.** It is the only copy of a pushed punch
  between arriving and reaching the LMS, and the deploy job runs
  `--force-recreate`.
- **`/api/health` returns 503 when the LMS is unreachable.** Point monitoring
  at it, not the container healthcheck — restarting on it would bounce the
  service exactly when it should stay up holding punches.
- **Use the dedicated database role.** `deploy/lms_bridge_role.sql` creates
  `biometric_bridge` with SELECT + column-scoped UPDATE on `biometric_devices`
  and INSERT + SELECT on `biometric_punch_log` — and nothing else. It cannot
  reach `staff_punches` or `biometric_enrollments`. This credential sits in a
  `.env` on a box next to a fingerprint reader; assume it leaks, and decide
  there what leaking costs.

## What was removed, and why

This service was started from a generic FastAPI scaffold and carried a lot of
it unused. Removed: Celery and its sample job, the demo `User` model and
schema, SQLAlchemy/asyncpg and alembic (this service owns no tables), a
duplicate `setup_logger` that made every line log twice, and the standalone
`live_device_capture.py` superseded by `app/sync/device_worker.py`. All of it
is in git history.
