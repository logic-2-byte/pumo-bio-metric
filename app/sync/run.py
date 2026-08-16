"""
Run the bridge on its own, with `python -m app.sync.run`.

For a branch box whose only job is to sit on the LAN, talk to the reader and
write to the LMS. No web server, no dashboard, no ADMS listener — just the
sync, which is the part that has to be running for attendance to exist.

Deploy this rather than the full FastAPI app wherever the dashboard is not
wanted: fewer moving parts on a machine nobody logs into, and one less port
open on a network that has a fingerprint reader on it.
"""
from __future__ import annotations

import signal
import sys
import threading

from app.sync.config import config
from app.sync.supervisor import SyncSupervisor


def main() -> int:
    print("=" * 66)
    print("  Biometric → LMS bridge")
    print("=" * 66)
    if not config.configured:
        print("  LMS database is not configured.")
        print("  Set LMS_DB_HOST, LMS_DB_NAME, LMS_DB_USER and LMS_DB_PASSWORD.")
        return 2
    print(f"  LMS database : {config.user}@{config.host}:{config.port}/{config.name}")
    print(f"  Poll         : every {config.poll_seconds}s")
    print(f"  Full sweep   : every {config.full_sync_seconds}s")
    print(f"  Heartbeat    : every {config.heartbeat_seconds}s")
    print(f"  Spool        : {config.spool_path}")
    print("=" * 66)

    supervisor = SyncSupervisor(config)
    supervisor.start()

    done = threading.Event()

    def shutdown(_signum: int, _frame: object) -> None:
        print("\nStopping…", flush=True)
        done.set()

    # Handled explicitly so a container stop drains cleanly rather than killing
    # a worker mid-batch. Nothing is actually lost either way — the reader keeps
    # its own copy and the next sweep re-offers it — but a clean stop keeps the
    # logs honest about what happened.
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    done.wait()
    supervisor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
