"""One punch, in the shape `biometric_punch_log` stores it."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Punch:
    """
    A single reader event, already in the LMS's column names.

    Frozen because a punch is a fact about something that happened; nothing
    downstream has any business editing one after it is read off the device.

    `device_time` is NAIVE and must stay that way. The reader has no notion of
    a timezone — it reports the wall clock in the room it is bolted to — and
    attaching one here would mean this bridge guessing on behalf of the LMS,
    which knows the answer. See the column comment in V44__biometric_devices.sql.
    """

    device_serial: str
    device_user_id: str
    device_time: datetime
    punch_state: int = 0
    verify_mode: int | None = None
    work_code: str | None = None
    device_user_name: str | None = None
    device_id: int | None = None
    raw_payload: str | None = None

    # ------------------------------------------------------------------
    # Spool round-tripping
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps({
            "device_serial": self.device_serial,
            "device_user_id": self.device_user_id,
            "device_time": self.device_time.strftime("%Y-%m-%d %H:%M:%S"),
            "punch_state": self.punch_state,
            "verify_mode": self.verify_mode,
            "work_code": self.work_code,
            "device_user_name": self.device_user_name,
            "device_id": self.device_id,
            "raw_payload": self.raw_payload,
        })

    @staticmethod
    def from_json(line: str) -> Punch:
        raw = json.loads(line)
        return Punch(
            device_serial=raw["device_serial"],
            device_user_id=str(raw["device_user_id"]),
            device_time=datetime.strptime(raw["device_time"], "%Y-%m-%d %H:%M:%S"),
            punch_state=int(raw.get("punch_state") or 0),
            verify_mode=raw.get("verify_mode"),
            work_code=raw.get("work_code"),
            device_user_name=raw.get("device_user_name"),
            device_id=raw.get("device_id"),
            raw_payload=raw.get("raw_payload"),
        )

    @property
    def key(self) -> tuple[str, str, datetime]:
        """The same triple the database's unique constraint uses."""
        return (self.device_serial, self.device_user_id, self.device_time)
