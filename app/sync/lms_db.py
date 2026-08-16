"""
The LMS database, and the only three things this bridge does to it.

  1. read `biometric_devices` to learn which readers to talk to
  2. insert into `biometric_punch_log`
  3. stamp a heartbeat so the console can tell a quiet reader from a dead one

Nothing here writes attendance. The LMS reconciler turns these raw rows into
`staff_punches` on its own schedule; this bridge deliberately knows nothing
about employees, work dates or directions, because a second program deciding
what a punch means is how the two end up disagreeing.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import Any

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
    # 1. Which readers to talk to
    # ------------------------------------------------------------------

    def active_devices(self) -> list[dict[str, Any]]:
        """
        The readers the console says are in service.

        The console is where an operator registers a device, so the console is
        where the list comes from — adding one there is picked up here within a
        couple of minutes without touching a config file or restarting anything.

        DISABLED readers are excluded by the query: disabling one is how you
        take a reader out of service, and a bridge that kept polling it would
        make that button do nothing visible.
        """
        with self._connection() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id, name, serial_no, ip_address, port, branch_id
                  FROM biometric_devices
                 WHERE status = 'ACTIVE'
                   AND ip_address IS NOT NULL
                 ORDER BY id
                """
            )
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # 2. The punches
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

    def count_stored(self, serial_no: str, since: datetime) -> int:
        """
        How many rows the LMS holds for this reader since a given time.

        The verification half of the sync. The device says it has N punches in
        that window; if this returns fewer, something was dropped and the next
        full sweep needs to say so out loud rather than reporting success.
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM biometric_punch_log
                 WHERE device_serial = %s AND device_time >= %s
                """,
                (serial_no, since),
            )
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------------
    # 3. Liveness
    # ------------------------------------------------------------------

    def heartbeat(self, serial_no: str, *, ip_address: str | None = None,
                  model: str | None = None, firmware: str | None = None) -> None:
        """
        Record that we just spoke to this reader.

        Clearing `offline_alerted_at` here is not incidental — it is what re-arms
        the LMS watchdog. Without it the next genuine outage would inherit this
        one's silence and never warn.
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

    def record_error(self, serial_no: str, message: str) -> None:
        """
        A failed attempt to reach a reader.

        Deliberately does NOT touch `last_seen_at`: the bridge is alive, the
        reader is not, and moving the heartbeat here would make a dead device
        look healthy for as long as this service keeps failing to reach it.
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
