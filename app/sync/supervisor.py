"""
Keeps one worker running per registered reader.

WHERE THE DEVICE LIST COMES FROM, and why it is not a config file: the console
is where an operator registers a reader, gives it a name and an address, and
says which branch it belongs to. Reading the list from `biometric_devices`
means adding a reader there is all it takes — no config edit, no restart, no
second place to keep in step. A device registered at 10:00 is being polled by
about 10:02.

There is no env-configured device and no local device list. A company runs one
or two readers per branch, so which readers exist is a property of the company,
not of the host this process happens to run on — and two places to define a
reader is one place to define it wrongly.

That means an empty `biometric_devices` table means nothing is polled, which is
correct: a reader nobody has registered is a reader nobody has told us the
serial of, and guessing it produces punches filed against an unknown device.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.sync.config import SyncConfig
from app.sync.config import config as default_config
from app.sync.device_worker import DeviceWorker, _log
from app.sync.lms_db import LmsDatabase, LmsUnavailableError
from app.sync.models import Punch
from app.sync.spool import PunchSpool


def _broadcast_to_dashboard(punch: Punch) -> None:
    """
    Mirror a punch onto this service's own live dashboard.

    Imported inside the function on purpose. `app.router.iclock` pulls in
    FastAPI and the SSE machinery, and the sync package has to remain usable
    from a plain script with no web server running — `python -m app.sync.run`
    is a supported way to deploy this bridge on a branch box that has no reason
    to serve a dashboard at all.
    """
    from app.router.iclock import broadcast_punch

    broadcast_punch({
        "id": f"{punch.device_serial}-{punch.device_user_id}-{punch.device_time:%Y%m%d%H%M%S}",
        "sn": punch.device_serial,
        "userId": punch.device_user_id,
        "userName": punch.device_user_name or f"User {punch.device_user_id}",
        "timestamp": punch.device_time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": str(punch.punch_state),
        "verifyType": str(punch.verify_mode if punch.verify_mode is not None else 1),
        "workCode": punch.work_code or "0",
        "raw": punch.raw_payload,
        "receivedAt": datetime.now().isoformat(),
        "source": "LMS_BRIDGE",
    })


class SyncSupervisor:
    """Discovers readers, starts a worker for each, and replaces the dead ones."""

    def __init__(self, config: SyncConfig | None = None) -> None:
        self._config = config or default_config
        self._db = LmsDatabase(self._config)
        self._spool = PunchSpool(self._config.spool_path)
        self._stop = threading.Event()
        self._workers: dict[str, DeviceWorker] = {}
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
        self._thread = threading.Thread(target=self._supervise, name="biometric-supervisor",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._db.close()

    # ------------------------------------------------------------------
    # The supervision loop
    # ------------------------------------------------------------------

    def _supervise(self) -> None:
        announced_outage = False

        while not self._stop.is_set():
            try:
                devices = self._db.active_devices()
                if announced_outage:
                    _log("1;32", "Bridge", "LMS database is reachable again")
                    announced_outage = False
            except LmsUnavailableError as exc:
                if not announced_outage:
                    _log("1;31", "Bridge",
                         f"LMS database unreachable ({exc}). Already-running readers "
                         f"keep capturing; punches are held on disk and on the "
                         f"devices themselves.")
                    announced_outage = True
                # Keep the workers we already have and try again next tick.
                #
                # NOT an empty list: that would be read as "every reader was
                # unregistered" and would tear down every running worker the
                # moment the database hiccuped. An outage tells us nothing about
                # which readers exist, so the last known answer stands.
                #
                # Nothing is lost while this persists. The readers hold their own
                # punches, and the sweep that runs when the database returns
                # re-offers everything they have.
                self._stop.wait(self._config.device_refresh_seconds)
                continue

            self._reconcile_workers(devices)
            self._stop.wait(self._config.device_refresh_seconds)

        for worker in self._workers.values():
            worker.join(timeout=2)

    def _reconcile_workers(self, devices: list[dict[str, Any]]) -> None:
        """Start a worker for anything new, and drop the ones no longer listed."""
        wanted = {str(d["serial_no"]): d for d in devices if d.get("serial_no")}

        for serial, device in wanted.items():
            existing = self._workers.get(serial)
            if existing is not None and existing.is_alive():
                continue
            if existing is not None:
                # A worker only exits when its thread body returns, which it
                # does on an unrecoverable setup problem (no pyzk, no address).
                # Replacing it gives the next config change a chance to fix it.
                _log("1;33", "Bridge", f"{serial}: worker had stopped, restarting")
            worker = DeviceWorker(device, self._db, self._spool, self._config,
                                  self._stop, on_punch=_broadcast_to_dashboard)
            self._workers[serial] = worker
            worker.start()

        for serial in list(self._workers):
            if serial not in wanted:
                # Disabled or deleted in the console. The worker shares the
                # supervisor's stop event, so it is not interrupted mid-batch;
                # it is simply forgotten and will exit with everything else at
                # shutdown. Nothing it has already read is lost.
                _log("1;90", "Bridge", f"{serial}: no longer active, releasing worker")
                self._workers.pop(serial, None)

    # ------------------------------------------------------------------
    # For the dashboard
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._config.enabled,
            "configured": self._config.configured,
            "workers": sorted(self._workers),
            "alive": sorted(s for s, w in self._workers.items() if w.is_alive()),
            "spooled": self._spool.pending(),
        }

    def lms_reachable(self) -> bool:
        """
        Can the LMS database be reached right now?

        A live probe rather than a cached flag: the health endpoint is asked
        precisely when somebody suspects it cannot, and a value last refreshed
        two minutes ago is the one answer that is no use.
        """
        return self._db.healthy()


#: The one instance the FastAPI lifespan starts and stops.
supervisor = SyncSupervisor()
