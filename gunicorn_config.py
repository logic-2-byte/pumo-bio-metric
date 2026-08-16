"""
Gunicorn settings.

ONE WORKER, AND IT MUST STAY ONE.

The original reason is gone and the rule survives it. This process used to hold
a TCP socket open to each reader, and an eSSL/ZKTeco reader accepts one
connection at a time — so four workers meant four pollers fighting over one
device, each reading the others' grip as a dropped connection. That poller is
deleted; readers push to us now, and nothing here opens a socket to a device.

What still argues for one worker:

  * The spool. `app.sync.supervisor` flushes punches held on disk, and a second
    process running the same flush would race it — both draining the same file,
    each having to put back what the other took.
  * The dashboard. `PUNCH_LOGS`, `LAST_SYNC_TIMES` and the SSE subscriber list
    are per-process state, so with two workers a punch shows up on whichever
    half of the dashboard happened to receive it.
  * The workload does not want more. A handful of dashboard requests and a
    trickle of ADMS pushes, none of it CPU-bound.

If this ever needs to scale, the answer is one process per site, not more
workers per process.
"""

workers = 1
worker_class = "uvicorn.workers.UvicornWorker"
bind = "0.0.0.0:8000"

# Generous, because an ADMS push can arrive while the LMS connection is being
# re-established. The old 30s would kill the request mid-recovery, and a reader
# whose push failed does not send that punch again.
timeout = 120
graceful_timeout = 30

# Readers hold connections open between pushes; recycling them costs a
# reconnect for no benefit.
keepalive = 65

loglevel = "info"
accesslog = "-"
errorlog = "-"
