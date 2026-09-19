from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    debug: bool = False
    app_env: str = "development"

    # "hybrid" tries a direct TCP connection to the device first, falling back
    # to an ADMS-queued command (the local/on-prem dev default, where the LAN
    # is reachable). "adms_only" skips the TCP attempt entirely — for a cloud
    # deployment that can never reach a device's LAN, that attempt is a
    # guaranteed multi-second dead end on every create/delete call.
    device_transport_mode: str = "hybrid"

    # The business day's timezone — see app.core.clock. Must match the LMS's
    # APP_ATTENDANCE_ZONE. Never the host's zone: the server runs in the US.
    business_timezone: str = "Asia/Kolkata"

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug(cls, v: Any) -> bool:
        if v == "" or v is None:
            return False
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes", "on", "t")
        return bool(v)

    @field_validator("device_transport_mode", mode="before")
    @classmethod
    def parse_device_transport_mode(cls, v: Any) -> str:
        value = str(v or "hybrid").strip().lower()
        return value if value in ("hybrid", "adms_only") else "hybrid"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
