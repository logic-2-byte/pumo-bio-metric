"""
One thread per reader: stay connected, sync everything, never lose a punch.

THE FAILURE THIS FILE IS WRITTEN AROUND
---------------------------------------
A reader on a branch LAN loses contact constantly — Wi-Fi drops, the switch is
rebooted, someone unplugs it to hoover. While it is disconnected the reader
keeps recording punches in its own flash memory; it holds thousands. So the
punches are never actually lost at the reader. What is lost is our copy.

The recovery, therefore, is not clever: on every reconnect, and periodically
while connected, read the reader's ENTIRE memory and offer all of it to the
database. The unique constraint on
`(device_serial, device_user_id, device_time)` throws away everything already
stored and keeps whatever was missed. This is why the bridge needs no cursor,
no high-water mark and no record of what it last sent — all three are state
that can be wrong, and none of them survive a crash.

The same sweep also covers the other direction of failure. If the database was
down while the reader was up, the punches are still sitting in the reader's
memory and the next sweep picks them up. If both were up but this process was
killed mid-batch, same answer. One mechanism, every case.

VERIFYING IT ACTUALLY LANDED
----------------------------
"Synced" is only worth printing if it was checked. After a full sweep the
worker asks the database how many rows it holds for this reader in the window
it just offered, and compares. A shortfall is logged loudly rather than
swallowed, because the entire purpose of this bridge is that somebody can
trust the attendance it produces.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from app.sync.config import SyncConfig
from app.sync.lms_db import LmsDatabase, LmsUnavailableError
from app.sync.models import Punch
from app.sync.spool import PunchSpool

try:
    from zk import ZK
except ImportError:  # pragma: no cover - exercised only on a host without pyzk
    ZK = None


def _log(colour: str, tag: str, message: str) -> None:
    print(f"\033[{colour}m[{tag}]\033[0m {message}", flush=True)


class DeviceWorker(threading.Thread):
    """
    Owns the conversation with exactly one reader.

    A thread rather than a coroutine because `pyzk` is a blocking socket
    library with no async form; wrapping it would mean a thread anyway, with an
    event loop in between pretending otherwise.
    """

    def __init__(self, device: dict[str, Any], db: LmsDatabase, spool: PunchSpool,
                 config: SyncConfig, stop: threading.Event,
                 on_punch: Callable[[Punch], None] | None = None) -> None:
        serial = device["serial_no"]
        super().__init__(name=f"biometric-{serial}", daemon=True)
        self._device = device
        self._db = db
        self._spool = spool
        self._config = config
        # NOT `self._stop`. This class extends threading.Thread, which has its
        # own private `_stop()` method that join() calls internally — assigning
        # an Event over it makes every join() raise
        # "TypeError: 'Event' object is not callable" during shutdown.
        self._shutdown = stop
        # Feeds this service's own live dashboard. Injected rather than imported
        # so the sync package does not depend on the web layer, and so a punch
        # is read off the reader exactly once — these devices generally accept
        # a single socket, so a second poller for the dashboard would fight
        # this one for the connection.
        self._on_punch = on_punch

        self.serial: str = serial
        self._ip: str = device.get("ip_address") or ""
        self._port: int = int(device.get("port") or 4370)
        self._device_id = device.get("id")
        self._label: str = device.get("name") or serial

        #: Names the reader has for its enrolled users, refreshed on connect.
        #: A hint carried through to the console so an operator mapping PIN 102
        #: sees "Gowtham" beside it. Never used for matching.
        self._user_names: dict[str, str] = {}

        self._last_full_sync = 0.0
        self._last_heartbeat = 0.0
        self._backoff = config.retry_seconds

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def run(self) -> None:
        if ZK is None:
            _log("1;31", "Bridge", f"{self._label}: pyzk is not installed — cannot poll this reader")
            return
        if not self._ip:
            _log("1;31", "Bridge", f"{self._label}: no IP address recorded, skipping")
            return

        _log("1;36", "Bridge", f"{self._label} ({self._ip}:{self._port}) worker started")

        while not self._shutdown.is_set():
            connection = None
            try:
                zk = ZK(self._ip, port=self._port, timeout=10,
                        force_udp=False, ommit_ping=False)
                connection = zk.connect()
                self._backoff = self._config.retry_seconds
                _log("1;32", "Bridge", f"{self._label}: connected")

                already = self._on_connect(connection)
                self._serve(connection, already)

            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}".strip()
                _log("1;31", "Bridge", f"{self._label}: {message}")
                self._safe_record_error(message)
                self._sleep_backoff()
            finally:
                if connection is not None:
                    with suppress(Exception):
                        connection.disconnect()

        _log("1;90", "Bridge", f"{self._label}: worker stopped")

    # ------------------------------------------------------------------
    # On (re)connect — the recovery path
    # ------------------------------------------------------------------

    def _on_connect(self, connection: Any) -> list[Punch]:
        """
        :return: everything already on the reader, so the poll loop can start
            from a known baseline instead of announcing the reader's entire
            history as if it had just happened.
        """
        self._refresh_user_names(connection)
        self._heartbeat(connection, force=True)
        # The reconnect sweep. Everything the reader buffered while we were
        # away is in its memory right now, so this is the moment to take it.
        return self._full_sync(connection, reason="reconnect")

    # ------------------------------------------------------------------
    # Steady state
    # ------------------------------------------------------------------

    def _serve(self, connection: Any, already: list[Punch]) -> None:
        """
        Poll while the connection holds.

        `already` seeds the baseline with what the reconnect sweep just synced.
        Starting empty would make the very next poll treat the reader's whole
        stored history as new and announce every punch of the last month on the
        dashboard — harmless to the database, which would drop them all as
        duplicates, and thoroughly alarming to anybody watching the screen.

        Leaves on any read error rather than trying to nurse a half-dead
        socket: `run()` reconnects, and reconnecting triggers a full sweep,
        which is exactly the recovery this situation needs.
        """
        seen: set[tuple[str, str, datetime]] = {p.key for p in already}

        while not self._shutdown.is_set():
            time.sleep(self._config.poll_seconds)

            punches = self._read_attendance(connection)
            fresh = [p for p in punches if p.key not in seen]
            if fresh:
                self._push(fresh)
                seen.update(p.key for p in fresh)
                for punch in fresh:
                    _log("1;92", "Punch",
                         f"{self._label} · PIN {punch.device_user_id} "
                         f"({punch.device_user_name or 'unknown'}) at {punch.device_time}")
                    self._notify(punch)

            now = time.monotonic()
            if now - self._last_heartbeat >= self._config.heartbeat_seconds:
                self._heartbeat(connection)

            if now - self._last_full_sync >= self._config.full_sync_seconds:
                self._refresh_user_names(connection)
                # Rebase the baseline on what the sweep saw, rather than
                # letting `seen` grow for the life of the connection — and
                # without a second read of the device, which the sweep has
                # just done.
                seen = {p.key for p in self._full_sync(connection, reason="scheduled")}

    def _notify(self, punch: Punch) -> None:
        """
        Show the punch on this service's own dashboard.

        Best-effort and never allowed to matter: the dashboard is a convenience
        and the LMS row is the record. A failure here must not interrupt a sync
        that has already succeeded.
        """
        if self._on_punch is None:
            return
        try:
            self._on_punch(punch)
        except Exception as exc:
            _log("1;33", "Bridge", f"{self._label}: dashboard update failed ({exc})")

    # ------------------------------------------------------------------
    # The sweep, and its verification
    # ------------------------------------------------------------------

    def _full_sync(self, connection: Any, *, reason: str) -> list[Punch]:
        self._last_full_sync = time.monotonic()
        punches = self._read_attendance(connection)
        if not punches:
            _log("1;90", "Sync", f"{self._label}: {reason} sweep — reader memory is empty")
            return []

        offered, inserted = self._push(punches)
        recovered = "" if inserted == 0 else f", {inserted} recovered"
        _log("1;34", "Sync",
             f"{self._label}: {reason} sweep — {offered} punch(es) on the reader{recovered}")

        self._verify(punches)
        return punches

    def _verify(self, punches: list[Punch]) -> None:
        """
        Check the database actually holds what the reader just showed us.

        Compares counts over the window the sweep covered rather than row by
        row: the question being answered is "did anything get dropped", and a
        count answers it in one query instead of thousands.

        A shortfall is reported and left alone. It is not retried here — the
        next sweep re-offers everything regardless, so a transient failure
        fixes itself, and a persistent one deserves a human rather than a loop.
        """
        oldest = min(p.device_time for p in punches)
        # A second of slack: `device_time` has second resolution and a
        # boundary punch should not read as missing because of rounding.
        window_start = oldest - timedelta(seconds=1)
        expected = sum(1 for p in punches if p.device_time >= window_start)

        try:
            stored = self._db.count_stored(self.serial, window_start)
        except LmsUnavailableError as exc:
            _log("1;33", "Verify", f"{self._label}: could not verify — {exc}")
            return

        if stored >= expected:
            _log("1;32", "Verify",
                 f"{self._label}: {expected} punch(es) on the reader since "
                 f"{window_start:%d %b %H:%M}, {stored} stored — in sync")
        else:
            _log("1;31", "Verify",
                 f"{self._label}: MISMATCH — reader has {expected} punch(es) since "
                 f"{window_start:%d %b %H:%M} but the database holds {stored}. "
                 f"{expected - stored} missing; the next sweep will retry.")

    # ------------------------------------------------------------------
    # Reading the reader
    # ------------------------------------------------------------------

    def _read_attendance(self, connection: Any) -> list[Punch]:
        records = connection.get_attendance() or []
        punches: list[Punch] = []
        for record in records:
            pin = str(record.user_id).strip()
            if not pin:
                continue
            timestamp = record.timestamp
            if not isinstance(timestamp, datetime):
                continue
            # pyzk names these the opposite way round to how they read:
            # `.status` carries the verification mode (1 = fingerprint) and
            # `.punch` carries the attendance state (0 = check-in). Getting
            # them the wrong way round yields punches that all look like
            # check-ins from a card reader, which is plausible enough to go
            # unnoticed.
            verify_mode = getattr(record, "status", None)
            punch_state = getattr(record, "punch", 0)
            punches.append(Punch(
                device_serial=self.serial,
                device_id=self._device_id,
                device_user_id=pin,
                device_user_name=self._user_names.get(pin),
                # Truncated to whole seconds so the value matches the unique
                # constraint byte for byte on every pass. A stray microsecond
                # would make the same punch look new every sweep.
                device_time=timestamp.replace(microsecond=0),
                punch_state=int(punch_state or 0),
                verify_mode=None if verify_mode is None else int(verify_mode),
                work_code="0",
                raw_payload=f"{pin}\t{timestamp:%Y-%m-%d %H:%M:%S}\t{punch_state}\t{verify_mode}",
            ))
        return punches

    def _refresh_user_names(self, connection: Any) -> None:
        try:
            for user in connection.get_users() or []:
                name = (user.name or "").strip()
                if name:
                    self._user_names[str(user.user_id).strip()] = name
        except Exception as exc:
            # Names are decoration. A reader that will not list its users still
            # produces perfectly good punches, and refusing to sync them
            # because a cosmetic call failed would be absurd.
            _log("1;33", "Bridge", f"{self._label}: could not read user names ({exc})")

    # ------------------------------------------------------------------
    # Writing, with the spool behind it
    # ------------------------------------------------------------------

    def _push(self, punches: list[Punch]) -> tuple[int, int]:
        """
        Send a batch to the LMS, spooling it if the database is unreachable.

        Anything already spooled goes first, so punches reach the database in
        roughly the order they happened rather than the order the outages did.
        """
        try:
            waiting = self._spool.drain()
            if waiting:
                try:
                    _, recovered = self._db.save_punches(waiting)
                    _log("1;34", "Spool",
                         f"flushed {len(waiting)} held punch(es), {recovered} new")
                except LmsUnavailableError:
                    self._spool.restore(waiting)
                    raise
            return self._db.save_punches(punches)
        except LmsUnavailableError as exc:
            self._spool.add(punches)
            _log("1;33", "Spool",
                 f"{self._label}: database unreachable ({exc}); held "
                 f"{len(punches)} punch(es) on disk, {self._spool.pending()} waiting")
            return (len(punches), 0)

    # ------------------------------------------------------------------
    # Liveness and backoff
    # ------------------------------------------------------------------

    def _heartbeat(self, connection: Any, *, force: bool = False) -> None:
        self._last_heartbeat = time.monotonic()
        firmware = model = None
        try:
            firmware = connection.get_firmware_version()
            model = connection.get_device_name()
        except Exception:
            pass
        try:
            self._db.heartbeat(self.serial, ip_address=self._ip,
                               model=str(model) if model else None,
                               firmware=str(firmware) if firmware else None)
        except LmsUnavailableError as exc:
            if force:
                _log("1;33", "Bridge", f"{self._label}: heartbeat not recorded — {exc}")

    def _safe_record_error(self, message: str) -> None:
        # Both ends unreachable. Nothing useful to do and nowhere to say it;
        # the console will show the device as offline on its own, which is the
        # correct conclusion anyway.
        with suppress(LmsUnavailableError):
            self._db.record_error(self.serial, message)

    def _sleep_backoff(self) -> None:
        """
        Wait before reconnecting, backing off up to a ceiling.

        Capped rather than exponential-forever: a reader switched off over a
        long weekend must still be picked up within minutes of coming back, not
        hours later because the delay had doubled its way into the distance.
        """
        delay = self._backoff
        self._backoff = min(self._backoff * 2, self._config.max_retry_seconds)
        self._shutdown.wait(delay)
