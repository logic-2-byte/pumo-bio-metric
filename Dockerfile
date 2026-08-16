FROM python:3.12-slim

# No build toolchain. psycopg2-binary ships a wheel and pyzk is pure Python, so
# build-essential and libpq-dev — which the previous image installed to compile
# psycopg2 from source — are no longer needed. That removes a compiler from a
# container that sits on a LAN with a fingerprint reader on it.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# Dependencies first, so a code change does not re-run pip.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what actually runs. No tests, no tools/, no .env — configuration comes
# from the environment at run time, and baking a .env into an image is how
# database passwords end up in a registry.
COPY app/ ./app
COPY gunicorn_config.py .

# Where punches are held when the LMS cannot be reached, and where logs rotate.
#
# DECLARED AS A VOLUME ON PURPOSE. The spool is the only place a pushed punch
# exists between arriving and reaching the database; if it lives in the
# container's writable layer, `docker compose up -d --force-recreate` silently
# throws away attendance. Mount it on the host.
RUN mkdir -p /srv/app/spool /srv/app/data/logs
VOLUME ["/srv/app/spool", "/srv/app/data/logs"]

ENV LMS_SYNC_SPOOL=/srv/app/spool/pending_punches.jsonl

# Runs unprivileged. Nothing here needs root, and this container is reachable
# from a branch LAN.
RUN useradd --system --uid 10001 --home /srv/app biometric \
    && chown -R biometric:biometric /srv/app
USER biometric

EXPOSE 8000

# LIVENESS, deliberately — /api/pulse, not /api/health.
#
# An unhealthy container gets restarted, and /api/health returns 503 whenever
# the LMS is unreachable. Wiring that here would restart this service every
# time the link to the LMS drops, which is precisely when it must stay up:
# it is still capturing, and the punches it is holding on disk are lost time
# if the process is bounced. Use /api/health from monitoring, where the answer
# raises a page instead of pulling the plug.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/pulse', timeout=5).status==200 else 1)"

# gunicorn with the config in gunicorn_config.py — which pins workers to 1.
# Read the comment at the top of that file before changing this line; more than
# one worker means more than one process fighting for each reader's socket.
CMD ["gunicorn", "-c", "gunicorn_config.py", "app.main:app"]
