"""
The other way a punch arrives: the reader pushes it to us.

Readers configured for ADMS/iClock POST their punches to this service instead
of waiting to be polled. Those punches reach `app.router.iclock`, not the
device workers, so they need their own path into the LMS.

WHY THIS ONE LEANS ON THE SPOOL HARDER. A polled reader can always be asked
again — its memory is right there, and the periodic sweep re-offers everything.
A pushed punch arrives exactly once. If the database is unreachable at that
moment and we drop it, the reader has no idea and will never send it again.
So a failed push is spooled, and the spool is flushed by whichever component
next reaches the database successfully.

Deliberately tolerant about parsing. `parse_attlog` in the router copes with
four wire formats because firmware varies; anything it produces that has a PIN
and a timestamp is worth storing, and anything it does not is skipped rather
than crashing a request the reader is waiting on. A reader that gets an error
back may retry forever or may drop the batch — neither is a thing to risk over
a malformed row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.sync.config import config
from app.sync.lms_db import LmsDatabase, LmsUnavailableError
from app.sync.models import Punch
from app.sync.spool import PunchSpool

#: Shared with the device workers, so a flush from either drains the same file.
_spool = PunchSpool(config.spool_path)
_db = LmsDatabase(config)

#: The formats eSSL/ZKTeco firmware has been seen to send, most common first.
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%Y%m%d%H%M%S",
)


def _parse_time(raw: str) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(microsecond=0)
        except ValueError:
            continue
    return None


def _as_int(raw: object, default: int | None = None) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def to_punch(record: dict[str, Any]) -> Punch | None:
    """Turn one parsed iClock row into a Punch, or None if it is unusable."""
    pin = str(record.get("userId") or "").strip()
    when = _parse_time(record.get("timestamp") or "")
    if not pin or when is None:
        return None

    serial = str(record.get("sn") or "").strip() or "UNKNOWN"
    name = record.get("userName")
    if isinstance(name, str) and name.startswith("User "):
        # The router substitutes "User 102" when it has no better name. That is
        # a placeholder, not something worth carrying into the console next to
        # an unmapped PIN, where it would read as the reader's own label.
        name = None

    return Punch(
        device_serial=serial,
        device_user_id=pin,
        device_time=when,
        punch_state=_as_int(record.get("status"), 0) or 0,
        verify_mode=_as_int(record.get("verifyType")),
        work_code=str(record.get("workCode") or "0"),
        device_user_name=name,
        raw_payload=(record.get("raw") or None),
    )


def ingest(records: list[dict[str, Any]]) -> tuple[int, int]:
    """
    Store pushed punches in the LMS, spooling them if it is unreachable.

    Never raises. The caller is inside a request the reader is waiting on, and
    the correct response to that reader is always "OK" — the punch is safe in
    the spool either way, and an error would only make the firmware improvise.

    :return: (usable rows, rows newly stored)
    """
    if not config.enabled or not config.configured:
        return (0, 0)

    punches = [p for p in (to_punch(r) for r in records) if p is not None]
    if not punches:
        return (0, 0)

    try:
        waiting = _spool.drain()
        if waiting:
            try:
                _db.save_punches(waiting)
            except LmsUnavailableError:
                _spool.restore(waiting)
                raise
        _, inserted = _db.save_punches(punches)
        return (len(punches), inserted)
    except LmsUnavailableError as exc:
        _spool.add(punches)
        print(f"\033[1;33m[Spool]\033[0m LMS unreachable ({exc}); held "
              f"{len(punches)} pushed punch(es), {_spool.pending()} waiting",
              flush=True)
        return (len(punches), 0)
    except Exception as exc:
        # Anything unforeseen still must not fail the reader's request.
        _spool.add(punches)
        print(f"\033[1;31m[Spool]\033[0m Unexpected error storing pushed punches "
              f"({type(exc).__name__}: {exc}); held {len(punches)} on disk", flush=True)
        return (len(punches), 0)
