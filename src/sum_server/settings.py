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

    # Presence derivation windows (see hosts/presence.py).
    presence_online_window_seconds: int = 90
    presence_reboot_grace_seconds: int = 900

    # --- Expired-enrollment cleanup (see hosts/cleanup.py) ---
    # Deletes host records whose enrollment was never used, long after the
    # token stopped working. The grace period is deliberately far longer than
    # `enrollment_token_ttl_seconds`: an expired token means the token no
    # longer works, not that the operator has given up on the machine.
    stale_host_cleanup_enabled: bool = True
    stale_host_cleanup_interval_seconds: int = Field(default=3600, ge=60)
    stale_host_grace_seconds: int = Field(default=7 * 24 * 3600, ge=3600)

    # Public base URL shown in enrollment instructions (falls back to the
    # request's base URL when empty), e.g. "https://sum.example.com".
    external_url: str = ""

    # --- Updates (GitHub release checking + self-update) ---
    update_check_enabled: bool = True
    update_check_interval_seconds: int = Field(default=21600, ge=300)
    github_owner: str = "opensum-ops"
    github_repo_server: str = "sum_server"
    github_repo_agent: str = "sum_agent"
    github_token: str = ""  # optional; raises the anonymous rate limit
    # Where cached agent binaries (and DB dumps for self-update) live.
    data_dir: Path = Path("/var/lib/sum-server")
    # Git checkout path for server self-update; blank disables self-update.
    install_dir: str = ""
    # systemd unit restarted by the self-updater.
    service_name: str = "sum-server"
    # Absolute path to the `uv` binary. Blank means "discover it" (see
    # updates/system.py::resolve_uv). Set this when uv lives somewhere the
    # systemd unit's PATH does not cover, e.g. /root/.local/bin/uv.
    uv_bin: str = ""

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
    return Settings()
