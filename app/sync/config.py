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

    #: How often to ask the reader for new punches.
    poll_seconds: int = 5

    #: How often to re-read the reader's ENTIRE memory rather than just the
    #: tail. This is the safety net that makes every outage self-healing: the
    #: device keeps its own log, so a full sweep re-offers everything it holds
    #: and the unique constraint drops whatever already landed. Anything the
    #: bridge missed while the network, the database or this process was down
    #: is recovered by the next sweep without anybody intervening.
    full_sync_seconds: int = 900

    #: How often to write the heartbeat the console's online dot reads.
    #: Comfortably inside the LMS default grace period of 15 minutes.
    heartbeat_seconds: int = 60

    #: Backoff between reconnect attempts to a reader that is not answering.
    retry_seconds: int = 15
    max_retry_seconds: int = 300

    #: How often to re-read `biometric_devices`, so a reader registered in the
    #: console is picked up without restarting this service.
    device_refresh_seconds: int = 120

    # ------------------------------------------------------------------
    # Fallbacks
    # ------------------------------------------------------------------

    #: Where punches go when the LMS database itself cannot be reached. The
    #: device's own memory is the primary safety net; this is the second one,
    #: for the case where the reader is fine and Postgres is not.
    spool_path: str = "spool/pending_punches.jsonl"

    # THERE IS NO DEVICE CONFIGURATION HERE, deliberately.
    #
    # Readers are registered in the LMS console and live in `biometric_devices`
    # — serial, address, port, branch and all. This service reads that table and
    # holds no opinion of its own about which devices exist.
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
        poll_seconds=_int("LMS_SYNC_POLL_SECONDS", 5),
        full_sync_seconds=_int("LMS_SYNC_FULL_SECONDS", 900),
        heartbeat_seconds=_int("LMS_SYNC_HEARTBEAT_SECONDS", 60),
        retry_seconds=_int("LMS_SYNC_RETRY_SECONDS", 15),
        max_retry_seconds=_int("LMS_SYNC_MAX_RETRY_SECONDS", 300),
        device_refresh_seconds=_int("LMS_SYNC_DEVICE_REFRESH_SECONDS", 120),
        spool_path=os.getenv("LMS_SYNC_SPOOL", "spool/pending_punches.jsonl").strip(),
    )


config = load_config()
