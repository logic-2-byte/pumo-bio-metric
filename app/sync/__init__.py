"""
Sync: getting pushed punches into the LMS database, and recording contact.

WHICH WAY THE TRAFFIC GOES. Readers are configured with this service's address
and connect to it (ADMS/iClock). Nothing in this package connects to a reader:
a device sits on a branch LAN behind a router that a server cannot route back
into, so a probe fails on a healthy unit and costs egress to say so. The old
`device_worker.py`, which dialled each reader on TCP 4370 through `pyzk` with
an ICMP ping in front of every connect, is deleted for that reason.

What is left:

  * `push_ingest` — punches arriving on the iClock routes, into Postgres
  * `liveness`    — writing down that a reader called in, throttled
  * `spool`       — the only copy of a pushed punch while Postgres is down
  * `supervisor`  — flushing that spool when Postgres comes back

This service writes to the LMS database DIRECTLY — it does not call the LMS
API. That is deliberate: capture has to keep working while the backend is
redeploying, being restarted, or simply unreachable, and the cheapest way to
guarantee that is to make capture depend on nothing but Postgres.

The contract is one table, `biometric_punch_log`, created by the LMS migration
`V44__biometric_devices.sql`. Read the comments in that file before changing
anything here; in particular the unique constraint

    (device_serial, device_user_id, device_time)

is what lets this bridge stay simple. Every write is an
`INSERT ... ON CONFLICT DO NOTHING`, so re-offering a punch is free and the
bridge never has to remember what it already sent.
"""
