"""
Liveness and readiness.

The health check used to `SELECT 1` against a local database this service no
longer has — it owns no tables, and everything it captures goes to the LMS.
Reporting on a connection nothing uses is worse than reporting nothing: it goes
green while the reader is unplugged and the punches are piling up on disk.

So health here answers the questions an operator actually has:

  * is the process up                      -> /api/pulse
  * is the LMS reachable                   -> /api/health
  * is anything stuck waiting to be sent   -> /api/health

"ARE THE READERS UP" IS NOT ONE OF THEM, and cannot be. Nothing here probes a
reader — it is behind a branch router this service cannot route to — so this
process has no way to answer that question at the moment it is asked. What it
knows is when each reader last called in, which is a different and better
answer, and it lives in the LMS console next to the reader rather than in a
health endpoint an orchestrator reads.
"""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.sync.supervisor import supervisor

router = APIRouter(prefix="/api", tags=["base"])


@router.get("/pulse")
def pulse() -> JSONResponse:
    """
    Liveness only: the process is running and serving.

    Deliberately checks nothing else. This is what a container orchestrator
    restarts on, and restarting the process because a reader on somebody's LAN
    is unplugged would turn a local cable fault into a service outage.
    """
    return JSONResponse(status_code=status.HTTP_200_OK, content={"message": "I'm Alive"})


@router.get("/health")
def health_check() -> JSONResponse:
    """
    Readiness: is this service actually doing its job?

    Returns 503 when this service cannot reach the LMS, because a bridge that
    cannot write is not ready in any useful sense — even though it is still
    capturing, and nothing is being lost while it holds punches on disk.

    A spool depth above zero is reported but is NOT unhealthy on its own: it is
    the mechanism working as designed. It matters only if it keeps growing,
    which is what the number is there for. It matters more than it used to,
    though: a pushed punch arrives once, so the spool is the only copy while
    Postgres is down.
    """
    state = supervisor.status()

    if not state["enabled"]:
        return JSONResponse(status_code=status.HTTP_200_OK,
                            content={"status": "disabled", **state})

    if not state["configured"]:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unconfigured",
                     "detail": "LMS_DB_HOST / LMS_DB_NAME / LMS_DB_USER are not set",
                     **state},
        )

    reachable = supervisor.lms_reachable()
    body = {
        "status": "ok" if reachable else "degraded",
        "lms_reachable": reachable,
        "punches_held_on_disk": state["spooled"],
    }
    code = status.HTTP_200_OK if reachable else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=body)
