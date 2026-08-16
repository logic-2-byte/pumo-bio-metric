"""Where the LMS database is, and how hard to try."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "t")


@dataclass(frozen=True)
class SyncConfig:
    """
    Configuration for the LMS bridge.

    The LMS connection is kept on its own `LMS_DB_*` prefix rather than reusing
    the `DB_*` variables this service already has. They are different databases
    owned by different applications, and one deployment quietly pointing the
    bridge at its own local Postgres — where the table simply does not exist —
    is a failure that looks exactly like "no punches are syncing".
    """

    enabled: bool = True

    host: str = ""
    port: int = 5432
    name: str = ""
    user: str = ""
    password: str = ""
    sslmode: str = "prefer"

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    #: The finest resolution at which a reader's contact is written to the LMS.
    #: Readers call in every few seconds; recording each one would be a dozen
    #: UPDATEs a minute per device to move a timestamp the console reads against
    #: a 15-minute grace period. See `app.sync.liveness` for the throttle.
    heartbeat_seconds: int = 60

    #: How often to try the spool again while the LMS database is down.
    #:
    #: Its own knob, and slower than the old poll loop, because it is not a
    #: capture path — capture is the readers pushing, which keeps working
    #: throughout. This is only how quickly a backlog clears once Postgres
    #: answers again, and hammering a database that is already struggling is
    #: not a way to make it answer sooner.
    spool_flush_seconds: int = 60

    # THE POLLING KNOBS ARE GONE: poll_seconds, full_sync_seconds,
    # retry_seconds, max_retry_seconds and device_refresh_seconds all described
    # an outbound loop that dialled each reader on TCP 4370. Readers push to us
    # now, so there is no interval to tune — the device decides how often it
    # calls, via `Delay` in the handshake this service answers with.

    # ------------------------------------------------------------------
    # Fallbacks
    # ------------------------------------------------------------------

    #: Where punches go when the LMS database cannot be reached.
    #:
    #: This matters more than it used to. A polled reader could always be asked
    #: again — its memory was right there and the next sweep re-offered
    #: everything. A pushed punch arrives exactly once: the reader considers it
    #: delivered and will never send it again, so this file is the only copy
    #: while Postgres is down.
    spool_path: str = "spool/pending_punches.jsonl"

    # THERE IS NO DEVICE CONFIGURATION HERE, deliberately.
    #
    # Readers are registered in the LMS console, and a reader announces itself
    # by the serial it sends when it calls in. This service holds no opinion of
    # its own about which devices exist and no longer even reads the list — it
    # writes down whatever serial arrives, and the console resolves it.
    #
    # An earlier version accepted a single DEVICE_IP/DEVICE_SN pair here as a
    # bootstrap fallback. It was removed: a company runs one or two readers per
    # branch, so "the device" is not a property of a host's env file, and two
    # places to define a reader is one place to define it wrongly. A serial
    # typed here that disagrees with the console produces punches filed against
    # an unknown device, which is silent until somebody reads the red list.

    @property
    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.name} "
            f"user={self.user} password={self.password} sslmode={self.sslmode}"
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.name and self.user)


def load_config() -> SyncConfig:
    return SyncConfig(
        enabled=_bool("LMS_SYNC_ENABLED", True),
        host=os.getenv("LMS_DB_HOST", "").strip(),
        port=_int("LMS_DB_PORT", 5432),
        name=os.getenv("LMS_DB_NAME", "").strip(),
        user=os.getenv("LMS_DB_USER", "").strip(),
        password=os.getenv("LMS_DB_PASSWORD", ""),
        sslmode=os.getenv("LMS_DB_SSLMODE", "prefer").strip() or "prefer",
        heartbeat_seconds=_int("LMS_SYNC_HEARTBEAT_SECONDS", 60),
        spool_flush_seconds=_int("LMS_SYNC_SPOOL_FLUSH_SECONDS", 60),
        spool_path=os.getenv("LMS_SYNC_SPOOL", "spool/pending_punches.jsonl").strip(),
    )


config = load_config()
