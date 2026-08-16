"""
Application startup and shutdown.

THERE IS ONLY ONE CAPTURE PATH, and it needs nothing started. Readers are
configured with this service's address and push to the ADMS/iClock routes in
`app.router.iclock`; those are HTTP handlers, so serving them *is* the capture.
Liveness rides along with them (`app.sync.liveness`), because a request from a
reader is the reader proving it is up.

WHAT WAS STARTED HERE BEFORE. A supervisor that ran one thread per registered
reader, each dialling the device on TCP 4370 through `pyzk` — with an ICMP ping
in front of every connect — polling its memory and stamping liveness down the
same socket. It is gone. In production a reader is on a branch LAN behind a
router this service cannot route to, so every connect failed on a device that
was working perfectly, and the probes cost egress to establish nothing.

The supervisor that remains does one unrelated job: flushing punches held on
disk when the LMS database was unreachable. See `app.sync.supervisor`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI

from app.router.iclock import get_local_ips
from app.sync.config import config as sync_config
from app.sync.supervisor import supervisor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[Any]:  # noqa: ARG001
    port = 8000
    local_ip = (get_local_ips() or ["127.0.0.1"])[0]

    print("\n" + "=" * 66)
    print("   \033[1;32mBiometric capture service\033[0m")
    print("=" * 66)
    print(f" Dashboard     : \033[1;34mhttp://localhost:{port}\033[0m")
    print(f" ADMS / iClock : \033[1;36mhttp://{local_ip}:{port}/iclock/cdata\033[0m")
    print("                 (point push-protocol readers here)")

    if sync_config.configured:
        print(f"\n LMS sync      : \033[1;32mON\033[0m -> "
              f"{sync_config.host}:{sync_config.port}/{sync_config.name}")
        print(" Liveness      : from the readers' own calls, recorded at most "
              f"every {sync_config.heartbeat_seconds}s")
        print(" Nothing is polled or pinged — point each reader at the address "
              "above and it connects itself")
    else:
        print("\n LMS sync      : \033[1;31mNOT CONFIGURED\033[0m — set LMS_DB_HOST, "
              "LMS_DB_NAME, LMS_DB_USER, LMS_DB_PASSWORD")
    print("=" * 66 + "\n")

    supervisor.start()

    yield

    supervisor.stop()
    # Drains the liveness writer so a stamp in flight is not lost on shutdown.
    with suppress(Exception):
        from app.sync.liveness import shutdown as stop_liveness

        stop_liveness()
