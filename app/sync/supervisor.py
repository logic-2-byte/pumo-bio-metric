"""
The background half of the service: keep the spool draining, and report state.

WHAT THIS USED TO BE. One thread per registered reader, each dialling the
device on TCP 4370 (`pyzk`, with an ICMP ping in front of every connect),
re-reading its whole memory on a schedule, and stamping a heartbeat down the
same socket. All of it is gone, along with `device_worker.py`, because the
premise was wrong: in production a reader is on a branch LAN behind a router
this service cannot route to, so every one of those connects failed on a
device that was working perfectly — while costing a probe per reader per few
seconds to find that out.

Readers push to us instead. Capture is `app.router.iclock` answering requests
the device makes, and liveness is `app.sync.liveness` writing down that they
arrived. Neither needs supervising, which is why almost nothing is left here.

WHAT IS LEFT, AND WHY IT IS STILL NEEDED. A pushed punch arrives exactly once.
If the LMS database is unreachable at that moment, `push_ingest` holds it on
disk rather than dropping it — the reader will never offer it again. Something
then has to notice the database is back and flush that file, and it cannot be
the next push: a branch where nobody punches after 18:00 would leave the
evening's held punches sitting there until somebody arrives the next morning.
So one timer, doing one thing.
"""
from __future__ import annotations

import threading
from typing import Any

from app.sync.config import SyncConfig
from app.sync.config import config as default_config
from app.sync.lms_db import LmsDatabase, LmsUnavailableError
from app.sync.spool import PunchSpool


def _log(colour: str, tag: str, message: str) -> None:
    print(f"\033[{colour}m[{tag}]\033[0m {message}", flush=True)


class SyncSupervisor:
    """Drains the spool when the LMS comes back, and answers the health check."""

    def __init__(self, config: SyncConfig | None = None) -> None:
        self._config = config or default_config
        self._db = LmsDatabase(self._config)
        self._spool = PunchSpool(self._config.spool_path)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._config.enabled:
            _log("1;90", "Bridge", "LMS sync is disabled (LMS_SYNC_ENABLED=false)")
            return
        if not self._config.configured:
            _log("1;33", "Bridge",
                 "LMS sync is not configured — set LMS_DB_HOST, LMS_DB_NAME, "
                 "LMS_DB_USER and LMS_DB_PASSWORD. Capture still works locally; "
                 "nothing is being written to the LMS.")
            return

        held = self._spool.pending()
        if held:
            _log("1;33", "Spool",
                 f"{held} punch(es) held from a previous run; they will be sent "
                 f"as soon as the database answers")

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="biometric-spool-flush",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._db.close()

    # ------------------------------------------------------------------
    # The flush loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        announced_outage = False

        while not self._stop.is_set():
            self._stop.wait(self._config.spool_flush_seconds)
            if self._stop.is_set():
                break
            if not self._spool.pending():
                continue

            try:
                self._flush()
                if announced_outage:
                    _log("1;32", "Bridge", "LMS database is reachable again")
                    announced_outage = False
            except LmsUnavailableError as exc:
                if not announced_outage:
                    _log("1;31", "Bridge",
                         f"LMS database unreachable ({exc}). Punches pushed by the "
                         f"readers are being held on disk and will be sent when it "
                         f"answers; nothing is being lost.")
                    announced_outage = True

    def _flush(self) -> None:
        """
        Send everything held on disk, putting it back if the database refuses.

        Restoring on failure is the whole safety property. A pushed punch has no
        second source — the reader considers it delivered and will never offer
        it again — so a batch that leaves the spool and does not land in
        Postgres has to go back into the spool, not into a log line.
        """
        waiting = self._spool.drain()
        if not waiting:
            return
        try:
            _, stored = self._db.save_punches(waiting)
        except LmsUnavailableError:
            self._spool.restore(waiting)
            raise
        except Exception:
            self._spool.restore(waiting)
            raise
        _log("1;34", "Spool", f"flushed {len(waiting)} held punch(es), {stored} new")

    # ------------------------------------------------------------------
    # For the health check
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._config.enabled,
            "configured": self._config.configured,
            "spooled": self._spool.pending(),
        }

    def lms_reachable(self) -> bool:
        """
        Can the LMS database be reached right now?

        A live probe rather than a cached flag: the health endpoint is asked
        precisely when somebody suspects it cannot, and a value last refreshed
        two minutes ago is the one answer that is no use.

        This is a probe of the LMS database, which is a server we own and can
        route to — not of a reader, which is the thing that cannot be probed.
        """
        return self._db.healthy()


#: The one instance the FastAPI lifespan starts and stops.
supervisor = SyncSupervisor()
