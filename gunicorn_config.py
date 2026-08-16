"""
Gunicorn settings.

ONE WORKER, AND IT MUST STAY ONE.

This is not a tuning preference. The process holds the socket to each
biometric reader and runs the sync threads behind it, and an eSSL/ZKTeco
reader generally accepts a single TCP connection at a time. Four workers —
which is what this file said before — means four supervisors, each opening its
own connection to the same reader, each reading the others' grip as a dropped
connection. The readers flap, the heartbeat lies, and the reconnect sweeps
trample each other.

The workload does not want more workers anyway: this service serves a handful
of dashboard requests and a trickle of ADMS pushes. The real work happens on
background threads that a second process would only duplicate.

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
