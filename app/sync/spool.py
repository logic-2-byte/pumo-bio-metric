"""
Punches held on local disk while the LMS database is unreachable.

THE SECOND SAFETY NET, not the first. The reader's own memory is the primary
one: it keeps thousands of punches and this bridge re-reads all of them on a
schedule, so almost every outage heals itself with no spool involved at all.

The spool exists for the gap that leaves. An iClock/ADMS push arrives once and
is not stored on the device in a form we can re-read on demand, and a reader
whose memory rolls over during a long database outage would lose its oldest
entries. In both cases the punch has been seen exactly once, by this process,
and dropping it because Postgres is restarting would be the one genuinely
unrecoverable failure in the chain.

Append-only JSONL, one punch per line, fsynced. Deliberately not a database:
the whole point is to work when the database does not, and a spool with its
own failure modes would be a worse version of the problem it solves.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from app.sync.models import Punch


class PunchSpool:
    """
    A file of punches waiting for Postgres to come back.

    Thread-safe because every device worker is its own thread and they all
    share one spool. The lock covers whole-file operations, which are rare —
    the common path writes nothing here at all.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def __len__(self) -> int:
        return self.pending()

    def pending(self) -> int:
        if not self._path.exists():
            return 0
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    def add(self, punches: list[Punch]) -> None:
        """
        Hold these until the database is reachable.

        Flushed and fsynced rather than left to the OS: the reason a punch is
        in here at all is that something is already going wrong, and a spool
        that loses its contents to a power cut is not a safety net.
        """
        if not punches:
            return
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            for punch in punches:
                handle.write(punch.to_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def drain(self) -> list[Punch]:
        """
        Take everything out of the spool.

        The file is renamed aside before it is read, so a punch arriving during
        the drain lands in a fresh spool rather than in the batch being flushed
        — which would either lose it or send it twice.

        The caller is expected to `restore()` on failure. Sending twice is
        harmless (the database's unique constraint drops the duplicate); losing
        one is not, which is why this is a move rather than a truncate.
        """
        with self._lock:
            if not self._path.exists():
                return []
            staging = self._path.with_suffix(".draining")
            try:
                os.replace(self._path, staging)
            except OSError:
                return []

            punches: list[Punch] = []
            try:
                with staging.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            punches.append(Punch.from_json(line))
                        except (ValueError, KeyError):
                            # One corrupt line must not strand the rest. It is
                            # dropped rather than retried forever: it can never
                            # parse, so retrying is an infinite loop.
                            continue
            except OSError:
                return []
            finally:
                staging.unlink(missing_ok=True)
            return punches

    def restore(self, punches: list[Punch]) -> None:
        """Put a failed batch back, to be retried on the next pass."""
        self.add(punches)
