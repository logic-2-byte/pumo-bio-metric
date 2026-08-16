"""
Test environment.

THE IMPORTANT PART: the tests must never reach the real LMS database.

This is not hypothetical. `.env` points at the live LMS, and the ADMS endpoint
test posts a punch — so the moment the bridge was wired up, running the suite
started inserting rows into the real `biometric_punch_log`. Test data in a
production attendance table is exactly the sort of thing nobody notices until
somebody is querying it during a payroll dispute.

So the LMS connection is switched off here, before anything under `app` is
imported. `app.sync.config` reads the environment at import time and
`push_ingest.ingest()` returns immediately when sync is disabled, so nothing
downstream can open a connection even by accident.
"""

import os

from dotenv import load_dotenv

# Real config first, so everything else behaves as it does in development.
env_file = ".env.test" if os.getenv("ENV_MODE") == "test" else ".env"
load_dotenv(env_file)

# Then take the database away.
#
# Set rather than deleted: `configured` is False without a host, but an
# explicit LMS_SYNC_ENABLED=false is the switch the code is written around and
# it survives someone later adding a default host.
os.environ["LMS_SYNC_ENABLED"] = "false"
for key in ("LMS_DB_HOST", "LMS_DB_NAME", "LMS_DB_USER", "LMS_DB_PASSWORD"):
    os.environ.pop(key, None)

# Nothing is polled either, and that needs no help: the reader list comes from
# `biometric_devices`, which the disabled connection above cannot reach. There
# is no env-configured device to unset.

# Keep the spool out of the working tree.
os.environ["LMS_SYNC_SPOOL"] = os.path.join(
    os.environ.get("TEMP", "/tmp"), "biometric-test-spool.jsonl"
)
