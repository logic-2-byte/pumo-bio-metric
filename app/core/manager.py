"""
Application startup and shutdown.

Both capture paths are started here:

  1. the LMS bridge (`app.sync`), which polls readers over the pyzk socket and
     writes their punches into the LMS database
  2. the ADMS/iClock listener in `app.router.iclock`, which is just HTTP routes
     and needs nothing started

The single-device `direct_device_socket_worker` that used to live in this file
is gone. It has been replaced by `app.sync.device_worker`, which does what it
did — live capture, user-name sync, the dashboard feed — and additionally
writes to the LMS, recovers whatever a reader buffered while it was
unreachable, and verifies that what the reader holds is what the database
actually stored. Running both would have been actively harmful: these readers
generally accept a single TCP connection at a time, so two pollers fight over
it and each reads the other's grip as a dropped connection.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
        print(" Reader list   : from biometric_devices, refreshed every "
              f"{sync_config.device_refresh_seconds}s")
        print(f" Full sweep    : every {sync_config.full_sync_seconds}s "
              f"(recovers anything missed during an outage)")
    else:
        print("\n LMS sync      : \033[1;31mNOT CONFIGURED\033[0m — set LMS_DB_HOST, "
              "LMS_DB_NAME, LMS_DB_USER, LMS_DB_PASSWORD")
    print("=" * 66 + "\n")

    supervisor.start()

    yield

    supervisor.stop()
