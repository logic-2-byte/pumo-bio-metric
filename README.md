# Biometric capture service

Receives punches from eSSL/ZKTeco fingerprint readers and writes them into the
LMS database, where the LMS turns them into attendance.

```
reader ──(ADMS/iClock push)──▶ this service ──(INSERT)──▶ LMS  biometric_punch_log
reader ──(ADMS/iClock push)──▶                                        │
         the reader connects                                          ▼
         to US, never the                             LMS reconciler ──▶ staff_punches
         other way round
```

## The three things worth understanding before changing anything

**The traffic is inbound, and only inbound.** Each reader is given this
service's address in its own menu (Comm → Ethernet / Cloud Server, ADMS or push
mode) and connects out to us. Nothing here connects to a reader. That is not a
style choice — a reader sits on a branch LAN behind a router with no public
address and no port forward, so a server anywhere else simply cannot route to
it.

**It writes to the LMS database directly, not to the LMS API.** Capture has to
keep working while the backend is redeploying or unreachable, and the cheapest
way to guarantee that is to make capture depend on nothing but Postgres. The
LMS reconciles the raw rows on its own schedule.

**Liveness is contact, not a probe.** A reader calls `/iclock/getrequest` every
few seconds looking for commands, whether or not anybody has punched. Each of
those calls stamps `biometric_devices.last_seen_at` (throttled to once a
minute), and the LMS console calls a reader offline when that goes quiet for
longer than the grace period. There is nothing to ask and nobody to ask it —
the device volunteers the answer, for free.

### Duplicates are free

```sql
UNIQUE (device_serial, device_user_id, device_time)
```

Every insert is `ON CONFLICT DO NOTHING`, so re-offering a punch costs nothing
and this service needs no cursor and no high-water mark — both are state that
can be wrong and neither survives a crash.

### A pushed punch arrives exactly once

This is the one thing to be careful about. A reader considers a punch delivered
the moment we answer `OK` and will never send it again. So when the LMS
database is unreachable, the punch goes to `spool/` on disk rather than being
dropped, and `app/sync/supervisor.py` flushes that file once the database
answers. **The spool must be on a volume that survives a restart.**

## Layout

```
app/
  main.py           FastAPI app: dashboard + ADMS listener
  core/
    manager.py      startup/shutdown
    logger.py       loguru sinks (call setup_logger exactly once)
    settings.py
  router/
    base.py         /api/pulse (liveness), /api/health (readiness)
    iclock.py       ADMS/iClock endpoints + the local dashboard
  sync/             ── into the LMS ──
    config.py       LMS_DB_* and the two timings
    models.py       Punch — the shape biometric_punch_log stores
    lms_db.py       the only statements run against the LMS
    push_ingest.py  pushed punches → Postgres, spooling on failure
    liveness.py     writing down that a reader called in
    spool.py        disk fallback when Postgres is unreachable
    supervisor.py   flushes the spool when Postgres comes back
    run.py          deprecated; exits non-zero and explains why
tools/              on-site device diagnostics, not part of the service
tests/
```

## Running it

```bash
cp .env.example .env      # fill in LMS_DB_*
docker compose up -d
```

Then, on each reader: set the server address to this service and enable ADMS /
push. Nothing is configured at this end.

`python -m app.sync.run` no longer starts anything and exits non-zero. It used
to run the poller without a web server; with capture being the HTTP handlers,
a process with no listener captures nothing rather than less.

## Where it can run

Anywhere the **readers** can reach it. A cloud VM with a public address is
fine, and no VPN to the branch is needed — which is the whole point of the push
model.

The previous constraint is gone: this service used to also poll each reader on
TCP 4370, which needed routing *into* the branch LAN. On a cloud VM that half
was inert, every connect failing on a healthy device several times a second,
and those failures were surfacing in the console as readers going offline.

## Registering a reader

Readers are registered in the **LMS console**, under Attendance → Biometric
Readers. This service does not read that list — a reader announces itself by
the serial it sends, and the console resolves that serial to a device, a branch
and a set of PINs.

**The one field that must match exactly is the serial**, as printed on the
unit. Get it wrong and the punches still arrive and are still stored, but
against an unknown device: they show up in the console's red list as
`UNKNOWN_DEVICE` rather than as somebody's attendance.

A reader that is pushing but is not registered logs one line saying so and
keeps being captured. Nothing is lost — it simply cannot show as online until
its serial exists in the console.

## Operational notes

- **One process per site.** `gunicorn_config.py` pins `workers = 1`; the spool
  flush and the dashboard's in-memory state are both per-process. Read the
  comment there before changing it.
- **The spool must be on a volume.** It is the only copy of a pushed punch
  between arriving and reaching the LMS, and the deploy job runs
  `--force-recreate`.
- **`/api/health` returns 503 when the LMS is unreachable.** Point monitoring
  at it, not the container healthcheck — restarting on it would bounce the
  service exactly when it should stay up holding punches.
- **`/api/health` says nothing about whether readers are up**, and cannot: this
  process has no way to ask one. That question is answered by "when did it last
  call in", which lives in the LMS console next to the reader.
- **Use the dedicated database role.** `deploy/lms_bridge_role.sql` creates
  `biometric_bridge` with column-scoped UPDATE on `biometric_devices` and
  INSERT + SELECT on `biometric_punch_log` — and nothing else. It cannot reach
  `staff_punches` or `biometric_enrollments`. Assume this credential leaks, and
  decide what leaking costs.

## What was removed, and why

**The outbound poller** (`app/sync/device_worker.py`, and the reader-managing
half of `supervisor.py`). It opened a TCP connection to each reader on 4370 via
`pyzk` — with an ICMP ping in front of every connect — swept the device's whole
memory on a timer, and stamped liveness down the same socket. It cannot work in
production: the readers are behind branch routers, so every probe failed on a
healthy device while costing egress to find that out. `pyzk` went with it and
is no longer in `requirements.txt`; `tools/list_device_admins.py` still uses it
and is now explicitly a run-it-on-site-by-hand diagnostic.

Losing the poller loses the reader-memory sweep, which used to recover punches
missed during an outage. The spool replaces it for the case that actually
happens — the LMS being down while readers keep pushing.

Earlier removals, all in git history: Celery and its sample job, the demo
`User` model and schema, SQLAlchemy/asyncpg and alembic (this service owns no
tables), a duplicate `setup_logger` that made every line log twice, and the
standalone `live_device_capture.py`.
