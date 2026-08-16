"""
Deprecated entry point. Kept only to explain why it no longer does anything.

`python -m app.sync.run` used to start the bridge without a web server: a
branch box whose whole job was to sit on the LAN, poll the reader over TCP 4370
and write to the LMS. That mode cannot exist any more, and the reason is not a
refactor — it is the direction the traffic goes.

Readers are configured with this service's address and push to it. Capture is
therefore the HTTP handlers in `app.router.iclock` answering requests the device
makes; there is no polling loop to run instead of them. A process with no web
server listening does not capture less, it captures nothing.

Run the application:

    uvicorn app.main:app --host 0.0.0.0 --port 8000

then set that address as the server on each reader (ADMS/iClock push mode).

This file exits non-zero rather than being deleted outright because a
deployment somewhere may still invoke it from a systemd unit or a Dockerfile
CMD, and a unit that silently starts a process which captures nothing is the
worst possible outcome — attendance quietly stops and nobody hears about it
until payroll. Failing loudly is what gets the unit fixed.
"""
from __future__ import annotations

import sys


def main() -> int:
    print("=" * 70, file=sys.stderr)
    print("  app.sync.run no longer starts anything.", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(file=sys.stderr)
    print("  This service no longer polls readers. Readers push to it, so the", file=sys.stderr)
    print("  web application IS the capture path — there is nothing to run", file=sys.stderr)
    print("  without it.", file=sys.stderr)
    print(file=sys.stderr)
    print("  Start the service with:", file=sys.stderr)
    print(file=sys.stderr)
    print("      uvicorn app.main:app --host 0.0.0.0 --port 8000", file=sys.stderr)
    print(file=sys.stderr)
    print("  then point each reader at that address (ADMS / iClock push mode).", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
