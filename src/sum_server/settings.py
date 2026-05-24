"""Application settings (pydantic-settings).

All keys load from env with prefix ``SUM_SERVER_``. A single ``Settings`` instance
is built at startup and attached to ``app.state`` so tests can override it.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(StrEnum):
    dev = "dev"
    test = "test"
    prod = "prod"


class LogFormat(StrEnum):
    console = "console"
    json = "json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SUM_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(...)
    signing_private_key: str = Field(
        ...,
        description="Path to Ed25519 seed file, or 'inline:<base64-32-bytes>' for dev/test.",
    )

    session_token_ttl_seconds: int = 14 * 24 * 3600
    enrollment_token_ttl_seconds: int = 3600
    agent_token_ttl_seconds: int = 0  # 0 = no expiry
    job_default_ttl_seconds: int = 3600

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: LogFormat = LogFormat.console
    env: Env = Env.dev
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    @model_validator(mode="after")
    def _check_prod_invariants(self) -> Settings:
        if self.env == Env.prod:
            if self.signing_private_key.startswith("inline:"):
                raise ValueError("inline signing key is not allowed in prod")
            if not Path(self.signing_private_key).exists():
                raise ValueError(f"signing key path does not exist: {self.signing_private_key}")
        if self.bootstrap_admin_email and not self.bootstrap_admin_password:
            raise ValueError("BOOTSTRAP_ADMIN_EMAIL set without BOOTSTRAP_ADMIN_PASSWORD")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
