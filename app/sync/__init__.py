"""
Sync: getting punches off the readers and into the LMS database.

The bridge writes to the LMS database DIRECTLY — it does not call the LMS API.
That is deliberate and it is the whole reason this package exists: a reader on
a branch LAN has to keep capturing while the backend is redeploying, being
restarted, or simply unreachable, and the cheapest way to guarantee that is to
make capture depend on nothing but Postgres.

The contract is one table, `biometric_punch_log`, created by the LMS migration
`V44__biometric_devices.sql`. Read the comments in that file before changing
anything here; in particular the unique constraint

    (device_serial, device_user_id, device_time)

is what lets this bridge stay simple. Every write is an
`INSERT ... ON CONFLICT DO NOTHING`, so re-offering a punch is free and the
bridge never has to remember what it already sent.
"""
