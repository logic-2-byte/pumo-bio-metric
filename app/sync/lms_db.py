"""
The LMS database, and the only two things this service does to it.

  1. insert into `biometric_punch_log`
  2. stamp contact so the console can tell a quiet reader from a dead one

Nothing here writes attendance. The LMS reconciler turns these raw rows into
`staff_punches` on its own schedule; this service deliberately knows nothing
about employees, work dates or directions, because a second program deciding
what a punch means is how the two end up disagreeing.

WHAT USED TO BE HERE AND IS NOT ANY MORE: a read of `biometric_devices` to
learn which readers to dial, and a count query to verify a poll sweep. Both
belonged to the outbound model, where this service connected to each reader on
TCP 4370. Readers now call us, so there is no list to fetch — a device
announces itself by its serial when it arrives — and no sweep to verify,
because there is no re-reading of a device's memory to check against.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

from app.sync.config import SyncConfig
from app.sync.models import Punch


class LmsUnavailableError(RuntimeError):
    """The LMS database could not be reached. The caller should spool and retry."""


class LmsDatabase:
    """
    A small connection pool and the handful of statements the bridge runs.

    Pooled rather than a single long-lived connection: the device workers are
    threads, one per reader, and psycopg2 connections are not safe to share
    across them. The pool is created lazily so that this service still starts
    when the LMS database is down — capture is the job that must not depend on
    it, and refusing to boot would defeat the point.
    """

    def __init__(self, config: SyncConfig) -> None:
        self._config = config
        self._pool: pg_pool.ThreadedConnectionPool | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _ensure_pool(self) -> pg_pool.ThreadedConnectionPool:
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is None:
                if not self._config.configured:
                    raise LmsUnavailableError(
                        "LMS database is not configured — set LMS_DB_HOST, "
                        "LMS_DB_NAME and LMS_DB_USER"
                    )
                try:
                    self._pool = pg_pool.ThreadedConnectionPool(
                        minconn=1, maxconn=8, dsn=self._config.dsn,
                        connect_timeout=10,
                    )
                except psycopg2.Error as exc:
                    raise LmsUnavailableError(str(exc).strip()) from exc
        return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.closeall()
                finally:
                    self._pool = None

    @contextmanager
    def _connection(self) -> Iterator[psycopg2.extensions.connection]:
        pool = self._ensure_pool()
        conn = None
        try:
            conn = pool.getconn()
            yield conn
            conn.commit()
        except psycopg2.Error as exc:
            if conn is not None:
                with suppress(psycopg2.Error):
                    conn.rollback()
            # A dropped connection must not be handed back to the next caller
            # still broken. Throwing the pool away is blunt but it is the only
            # reliable way back from a Postgres restart or a failover.
            self.close()
            raise LmsUnavailableError(str(exc).strip()) from exc
        finally:
            if conn is not None and self._pool is not None:
                with suppress(psycopg2.Error, KeyError):
                    self._pool.putconn(conn)

    def healthy(self) -> bool:
        try:
            with self._connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                return True
        except LmsUnavailableError:
            return False

    # ------------------------------------------------------------------
    # 1. The punches
    # ------------------------------------------------------------------

    def save_punches(self, punches: Sequence[Punch]) -> tuple[int, int]:
        """
        Insert a batch, ignoring anything already there.

        `ON CONFLICT DO NOTHING` against
        `(device_serial, device_user_id, device_time)` is the whole reason this
        bridge needs no cursor, no high-water mark and no memory of what it has
        already sent. It can offer the reader's entire log every time and only
        the genuinely new rows land.

        `RETURNING id` is what makes the count honest: without it there is no
        way to tell "inserted 400" from "offered 400, all duplicates", and the
        difference is exactly what an operator needs after an outage.

        :return: (offered, actually inserted)
        """
        if not punches:
            return (0, 0)

        rows = [
            (
                p.device_serial, p.device_id, p.device_user_id, p.device_user_name,
                p.device_time, p.punch_state, p.verify_mode, p.work_code, p.raw_payload,
            )
            for p in punches
        ]
        with self._connection() as conn, conn.cursor() as cur:
            inserted = psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO biometric_punch_log
                    (device_serial, device_id, device_user_id, device_user_name,
                     device_time, punch_state, verify_mode, work_code, raw_payload)
                VALUES %s
                ON CONFLICT (device_serial, device_user_id, device_time) DO NOTHING
                RETURNING id
                """,
                rows,
                page_size=500,
                fetch=True,
            )
            return (len(rows), len(inserted))

    # ------------------------------------------------------------------
    # 2. Liveness
    # ------------------------------------------------------------------

    def heartbeat(self, serial_no: str, *, ip_address: str | None = None,
                  model: str | None = None, firmware: str | None = None) -> bool:
        """
        Record that this reader just called in.

        Contact reported after the fact, never a probe: the device opened the
        connection, and this is where that fact is written down. See
        `app.sync.liveness` for why there is nothing to probe.

        `ip_address` is the address it connected FROM, which is worth keeping
        current — a branch router hands out a new lease and the console should
        show where the unit actually is. Nothing connects to it.

        Clearing `offline_alerted_at` is not incidental: it is what re-arms the
        LMS watchdog. Without it the next genuine outage would inherit this
        one's silence and never warn. `last_error` is cleared for the same
        reason — a clean contact means whatever went wrong last time is no
        longer what is happening.

        :return: True if a registered reader was updated. False means no row
            carries this serial — the device is talking to us but nobody has
            registered it in the console.
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE biometric_devices
                   SET last_seen_at = now(),
                       offline_alerted_at = NULL,
                       last_error = NULL,
                       ip_address = COALESCE(%s, ip_address),
                       model = COALESCE(%s, model),
                       firmware = COALESCE(%s, firmware)
                 WHERE serial_no = %s
                """,
                (ip_address, model, firmware, serial_no),
            )
            return bool(cur.rowcount > 0)

    def record_traffic_error(self, serial_no: str, message: str) -> None:
        """
        Note that something in this reader's traffic could not be handled.

        Deliberately does NOT touch `last_seen_at`, but not for the reason its
        predecessor did not. The old `record_error` withheld contact because it
        meant "we tried to reach the device and failed", and stamping it would
        have made a dead reader look healthy. This one leaves the column alone
        simply because it is not its job: the device demonstrably called in, and
        `heartbeat` has already recorded that for this same request.
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE biometric_devices SET last_error = %s WHERE serial_no = %s",
                (message[:300], serial_no),
            )

    # `biometric_devices.last_punch_at` is deliberately NOT written here.
    #
    # The LMS reconciler already stamps it as it resolves each raw row, and it
    # is the component that can do so correctly: `device_time` is naive local
    # wall clock and `last_punch_at` is a timestamptz, so converting between
    # them needs the attendance timezone — which the LMS holds and this bridge
    # does not. Writing it from here would also mean a backfill of last week's
    # log claimed a punch had just happened.
