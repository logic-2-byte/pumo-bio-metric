-- The database role the biometric bridge connects as.
--
-- Run this ONCE against the LMS database, as a superuser, before pointing a
-- bridge at it. Replace the password.
--
--   psql -U postgres -d l2b_lms -f deploy/lms_bridge_role.sql
--
-- WHY A SEPARATE ROLE AND NOT `postgres`.
--
-- This credential lives in a .env file on a small box sitting on a branch LAN,
-- next to a fingerprint reader, in a room other people walk through. Assume it
-- leaks. What it can do when it leaks is decided here.
--
-- With these grants the worst case is: read the device list, fake a heartbeat,
-- and insert junk into the raw punch log — all of it visible in the console and
-- none of it able to change anybody's attendance, because the bridge cannot
-- reach staff_punches at all. The LMS reconciler is what turns a raw row into
-- attendance, and it applies its own rules (a PIN must be mapped, the device
-- must be registered and active) that this role cannot bypass.
--
-- With `postgres` the worst case is the entire company's data.

-- ---------------------------------------------------------------------
-- 1. The role
-- ---------------------------------------------------------------------
-- NOLOGIN would be pointless here and CREATEDB/SUPERUSER are deliberately
-- absent: this role exists to run four statements and nothing else.
CREATE ROLE biometric_bridge WITH LOGIN PASSWORD 'CHANGE_ME';

COMMENT ON ROLE biometric_bridge IS
    'The eSSL/ZKTeco capture bridge. Writes biometric_punch_log and stamps device '
    'heartbeats. Must never be able to reach staff_punches.';

-- ---------------------------------------------------------------------
-- 2. Exactly what it needs
-- ---------------------------------------------------------------------
GRANT CONNECT ON DATABASE l2b_lms TO biometric_bridge;
GRANT USAGE   ON SCHEMA public    TO biometric_bridge;

-- Read the reader list (which devices to poll), and stamp liveness on them.
-- UPDATE is column-scoped: the bridge reports what it observed, it does not get
-- to re-point a device at another branch or rename it.
GRANT SELECT ON biometric_devices TO biometric_bridge;
GRANT UPDATE (last_seen_at, last_error, offline_alerted_at, ip_address, model, firmware)
    ON biometric_devices TO biometric_bridge;

-- Write punches, and read back its own writes — the verification step counts
-- what actually landed against what the reader holds.
GRANT SELECT, INSERT ON biometric_punch_log TO biometric_bridge;

-- No sequence grant is needed: biometric_punch_log.id is
-- GENERATED ALWAYS AS IDENTITY, and INSERT covers it.

-- Deliberately NOT granted, and each one is load-bearing:
--   biometric_enrollments  — who a PIN belongs to is the LMS's decision. A
--                            bridge that could write here could assign a
--                            stranger's finger to any employee.
--   staff_punches          — the attendance record itself. Append-only payroll
--                            evidence; the bridge has no business near it.
--   UPDATE/DELETE on biometric_punch_log — the log is a verbatim mirror. The
--                            bridge writes what the reader said and never
--                            revises it. The LMS reconciler owns the
--                            resolution columns.
--   everything else in the schema.

-- ---------------------------------------------------------------------
-- 3. Check it came out right
-- ---------------------------------------------------------------------
-- Expect: t, t, t, f  — can read devices, can insert punches, can heartbeat,
-- cannot touch attendance.
SELECT
    has_table_privilege('biometric_bridge', 'biometric_devices',   'SELECT') AS reads_devices,
    has_table_privilege('biometric_bridge', 'biometric_punch_log', 'INSERT') AS writes_punches,
    has_column_privilege('biometric_bridge', 'biometric_devices', 'last_seen_at', 'UPDATE') AS heartbeats,
    has_table_privilege('biometric_bridge', 'staff_punches',       'SELECT') AS reaches_attendance;
