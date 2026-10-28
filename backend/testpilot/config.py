"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Optional operator file with KEY=VALUE lines (dotenv format). Real environment variables
# take precedence over it. Default location: ./config.local, override with TESTPILOT_CONFIG_LOCAL.
CONFIG_LOCAL = os.environ.get("TESTPILOT_CONFIG_LOCAL", "config.local")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=CONFIG_LOCAL, env_file_encoding="utf-8", extra="ignore",
                                      case_sensitive=False)

    # --- application -------------------------------------------------------
    app_env: Literal["production", "development", "test"] = "production"
    public_url: str = "https://testingagent.unical.in"
    allowed_origins: str = "https://testingagent.unical.in"  # comma separated
    api_root_path: str = ""  # nginx strips nothing; routes are mounted under /api

    # --- multi-tenancy -----------------------------------------------------
    # Tenant served by the legacy /api/... routes and old /projects/... links (release 1.0 data).
    default_tenant_slug: str = Field(default="unical", pattern=r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
    registration_enabled: bool = True
    registrations_per_ip_per_day: int = 5
    invitation_ttl_hours: int = 72

    # --- database ----------------------------------------------------------
    database_url: SecretStr = SecretStr("postgresql+psycopg://testpilot:change-me@db:5432/testpilot")
    db_pool_size: int = 5

    # --- secrets -----------------------------------------------------------
    session_secret: SecretStr = SecretStr("")  # used to pepper session token hashes
    credential_encryption_key: SecretStr = SecretStr("")  # Fernet key for project secrets
    session_ttl_hours: int = 12
    session_idle_minutes: int = 120
    cookie_secure: bool = True
    cookie_name: str = "tp_session"

    # --- Claude ------------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_workspace_id: str = ""  # only for user-level keys (sk-ant-usr-...) not scoped to a workspace
    claude_model: str = "claude-sonnet-5-5"
    claude_timeout_seconds: int = 90
    claude_max_tokens: int = 8000

    # --- evidence ----------------------------------------------------------
    evidence_dir: Path = Path("/data/evidence")
    capture_video: bool = True
    capture_trace: bool = True
    screenshot_mode: Literal["each_step", "on_failure"] = "each_step"

    # --- worker / execution limits ----------------------------------------
    worker_poll_seconds: float = 2.0
    max_concurrent_runs: int = 2
    run_timeout_seconds: int = 600
    step_timeout_ms: int = 15000
    max_step_retries: int = 2
    max_steps_per_case: int = 200
    worker_heartbeat_seconds: int = 10
    stale_run_minutes: int = 5
    navigation_timeout_ms: int = 30000

    # --- target safety -----------------------------------------------------
    # All browser traffic is forced through a per-run filtering egress proxy (egress_proxy.py).
    # Hostnames that may resolve to private addresses (e.g. the bundled sample app).
    # Operator-level only: cannot be changed from the UI.
    allowed_private_targets: str = ""
    # Organizations allowed to use ALLOWED_PRIVATE_TARGETS (comma separated slugs). Default: only the
    # default (Unical) organization. Customer organizations can never reach private addresses.
    private_target_tenants: str = ""

    # --- MySQL report store (copy of every finished run's report) ---------
    mysql_reports_enabled: bool = False
    mysql_reports_host: str = ""
    mysql_reports_port: int = 3306
    mysql_reports_database: str = ""
    mysql_reports_user: str = ""
    mysql_reports_password: SecretStr = SecretStr("")
    mysql_reports_ssl: Literal["disabled", "required", "verify"] = "disabled"
    mysql_reports_ssl_ca: str = ""  # CA bundle path when ssl=verify
    mysql_reports_table_prefix: str = Field(default="tp_", pattern=r"^[A-Za-z0-9_]{0,20}$")
    mysql_reports_max_html_mb: int = 12  # HTML+JSON above this: row stored without the HTML body
    mysql_reports_sync_interval_seconds: int = 60

    # --- logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("public_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def private_target_tenant_slugs(self) -> set[str]:
        extra = {h.strip().lower() for h in self.private_target_tenants.split(",") if h.strip()}
        return extra | {self.default_tenant_slug}

    @property
    def private_targets(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_private_targets.split(",") if h.strip()}

    @property
    def ai_configured(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value().strip())

    def problems(self) -> list[str]:
        """Return a list of production configuration problems (empty = OK)."""
        out: list[str] = []
        if self.app_env == "production":
            if os.environ.get("TESTPILOT_COMPONENT") != "worker" and len(self.session_secret.get_secret_value()) < 32:
                out.append("SESSION_SECRET must be at least 32 characters")
            if not self.credential_encryption_key.get_secret_value():
                out.append("CREDENTIAL_ENCRYPTION_KEY is not set")
            if not self.cookie_secure:
                out.append("COOKIE_SECURE must be true in production")
            if not self.public_url.startswith("https://"):
                out.append("PUBLIC_URL must use https in production")
            if "change-me" in self.database_url.get_secret_value():
                out.append("DATABASE_URL still contains the placeholder password")
            if self.public_url not in self.origins:
                out.append("ALLOWED_ORIGINS must include PUBLIC_URL")
        if self.mysql_reports_enabled:
            for k in ("host", "database", "user"):
                if not getattr(self, f"mysql_reports_{k}"):
                    out.append(f"MYSQL_REPORTS_{k.upper()} is required when MYSQL_REPORTS_ENABLED=true")
            if not self.mysql_reports_password.get_secret_value():
                out.append("MYSQL_REPORTS_PASSWORD is required when MYSQL_REPORTS_ENABLED=true")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
