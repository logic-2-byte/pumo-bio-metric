"""
Liveness, from the only evidence there is: the reader called us.

WHY THERE IS NO PROBE HERE
--------------------------
This service used to dial every reader on TCP 4370 — `pyzk` with
`ommit_ping=False`, so an ICMP ping before each connect — and treat a
successful connect as proof the device was alive. That cannot work in
production. A reader sits on a branch LAN behind a router with no public
address and no port forward, so a server anywhere else cannot route to it: the
probe fails on a perfectly healthy device, and running one every few seconds
per reader costs egress to produce an answer that is wrong by construction.

What replaces it is better anyway, and it was already arriving. A reader
configured for ADMS/iClock is given this service's address and calls in by
itself every few seconds whether or not anybody punched — that is what
`Delay=5` in the handshake asks it to do. Every one of those calls is proof the
device is up, delivered by the device, at no cost to us. So contact is
*recorded when it arrives* and never gone looking for.

THROTTLED, BECAUSE THE READERS ARE CHATTY
-----------------------------------------
A device polls `/iclock/getrequest` every few seconds. Stamping `last_seen_at`
on each one would be a dozen UPDATEs a minute per reader to move a timestamp
nobody reads at that resolution. The LMS calls a reader offline after 15
minutes by default, so one stamp a minute is already two orders of magnitude
finer than the question being asked.

OFF THE REQUEST PATH
--------------------
The write happens on a background thread. `psycopg2` is blocking and these
handlers are `async`, so stamping inline would park the event loop on a
database round trip while a reader waits — and the reader is the one thing
here that must never be kept waiting. Nothing downstream depends on the stamp
having landed by the time the handler answers.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from app.sync.config import config
from app.sync.lms_db import LmsDatabase, LmsUnavailableError

_db = LmsDatabase(config)

#: One thread, because these writes are tiny, rare after throttling, and have
#: no reason to overtake each other. A pool would only add ways for a burst of
#: reader traffic to open connections the pool behind it would then refuse.
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="liveness")

#: serial -> monotonic clock of the last stamp we actually wrote.
_last_write: dict[str, float] = {}
_lock = threading.Lock()

#: Serials we have already complained about, so a reader nobody registered in
#: the console produces one line in the log rather than one a minute forever.
_warned_unknown: set[str] = set()


def _should_write(serial: str, *, force: bool) -> bool:
    """Has enough time passed since this reader's last stamp?"""
    now = time.monotonic()
    with _lock:
        previous = _last_write.get(serial)
        if not force and previous is not None:
            if now - previous < config.heartbeat_seconds:
                return False
        _last_write[serial] = now
        return True


def _write(serial: str, ip_address: str | None) -> None:
    try:
        updated = _db.heartbeat(serial, ip_address=ip_address)
    except LmsUnavailableError as exc:
        # Not worth spooling. A heartbeat is only interesting while it is
        # current: by the time the database is back, the reader has called in
        # again and stamped a newer one. Forget it and let the throttle re-fire.
        with _lock:
            _last_write.pop(serial, None)
        print(f"\033[1;33m[Liveness]\033[0m {serial}: contact not recorded ({exc})", flush=True)
        return
    except Exception as exc:  # pragma: no cover - defensive
        print(f"\033[1;31m[Liveness]\033[0m {serial}: {type(exc).__name__}: {exc}", flush=True)
        return

    if not updated and serial not in _warned_unknown:
        # The reader is talking to us and the console has never heard of it.
        # Its punches still land against the serial alone and show up in the
        # LMS red list as UNKNOWN_DEVICE, so nothing is lost — but the console
        # cannot show it as online until somebody registers that serial, and
        # that is worth saying once.
        _warned_unknown.add(serial)
        print(f"\033[1;33m[Liveness]\033[0m {serial}: no reader registered with this serial "
              f"in the LMS console — its punches are being kept, but it cannot show as "
              f"online until it is registered", flush=True)


def mark_seen(serial: str | None, *, ip_address: str | None = None,
              force: bool = False) -> None:
    """
    Record that this reader just called in.

    Safe to call from every iClock handler on every request: the throttle makes
    all but one call a minute a dictionary lookup, and nothing here raises into
    the request the reader is waiting on.

    :param force: stamp regardless of the throttle. Used for the handshake,
        which is a reader announcing itself after a restart or a config change
        — the one contact worth recording the instant it happens.
    """
    if not serial or not config.enabled or not config.configured:
        return
    if not _should_write(serial, force=force):
        return
    _writer.submit(_write, serial, ip_address)


def record_traffic_error(serial: str | None, message: str) -> None:
    """
    Note that something in this reader's traffic could not be handled.

    NOT a failed connection — there are no connection attempts. The device
    called in and sent something unusable, which is a different problem with a
    different fix, and it deliberately does not suppress the contact that
    `mark_seen` recorded for the same request.
    """
    if not serial or not config.enabled or not config.configured:
        return
    _writer.submit(_record_error, serial, message)


def _record_error(serial: str, message: str) -> None:
    # Best effort by definition: this is a note about a note, and failing to
    # write it must not become a second thing that went wrong.
    with suppress(Exception):
        _db.record_traffic_error(serial, message)


def shutdown() -> None:
    """Drain the writer so a clean stop does not drop a stamp mid-flight."""
    _writer.shutdown(wait=True)
    _db.close()
