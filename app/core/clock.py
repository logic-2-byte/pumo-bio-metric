"""
The business clock.

The bridge is hosted in the US, but the readers, the people tapping them and
the LMS all live on Indian time. `datetime.now()` reads the host's clock, so a
naive timestamp written here meant US time to anybody who read it back — the
LMS parsed "last seen" in its own zone, and the 30-day recovery query asked a
reader in Chennai for a window ending ten and a half hours early.

Two readings, for two different purposes:

* `now()` — an aware instant in the business zone. Use it for anything that is
  stored, shown or sent: its `isoformat()` carries the offset
  (`2026-09-19T14:37:02+05:30`), which is unambiguous wherever it is read.
* `wall_now()` — the naive wall-clock time a reader would show. Use it only to
  compare with, or build, a reader's own timestamps (ATTLOG times, the
  `DATA QUERY ATTLOG` window), which are naive local times on the device.

The zone is `BUSINESS_TIMEZONE` (default `Asia/Kolkata`) and must match the
LMS's `APP_ATTENDANCE_ZONE`. Serving another country later is a change of that
variable, not of code.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.core.settings import settings


@lru_cache(maxsize=1)
def business_tz() -> ZoneInfo:
    return ZoneInfo(settings.business_timezone)


def now() -> datetime:
    """This instant, in the business zone, with its offset."""
    return datetime.now(business_tz())


def wall_now() -> datetime:
    """The business zone's wall clock, naive — the form a reader's timestamps take."""
    return now().replace(tzinfo=None)


def from_timestamp(ts: float) -> datetime:
    """A `time.time()` value as an aware business-zone datetime."""
    return datetime.fromtimestamp(ts, business_tz())
