"""Runtime configuration.

Two rules this module exists to enforce:

1. Secrets are never printable. Every credential field is a ``SecretStr`` and
   :meth:`Settings.safe_dump` reports *whether* a secret is set, never what it
   is, so ``/status`` and startup logs are safe to screenshot or paste into a
   bug report.
2. ``live`` mode cannot start with missing guards. ``shadow`` mode is the
   default: the service boots, queues, reconciles and reports, but performs no
   outbound Telegram action until the operator deliberately flips the mode.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AppMode", "Settings", "SENSITIVE_FIELDS", "load_settings"]

SENSITIVE_FIELDS = frozenset(
    {
        "database_url",
        "control_token",
        "telegram_api_hash",
        "telegram_session_string",
    }
)

_ID_LIST_RE = re.compile(r"[\s,;]+")
_MISSING = object()


class AppMode(str, Enum):
    """``shadow`` = no outbound Telegram actions. ``live`` = fully enabled."""

    SHADOW = "shadow"
    LIVE = "live"


def _parse_user_ids(value: Any) -> tuple[int, ...]:
    """Accept ``123, 456`` / ``[123,456]`` / ``123`` and return a sorted tuple."""
    if value is None:
        return ()
    if isinstance(value, int):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value]
    else:
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", '""', "''"}:
            return ()
        text = text.strip("[]")
        parts = _ID_LIST_RE.split(text)
    ids: set[int] = set()
    for part in parts:
        part = part.strip().strip('"').strip("'")
        if not part:
            continue
        try:
            parsed = int(part)
        except ValueError as exc:
            raise ValueError(
                f"Telegram user IDs must be numeric; got {part!r}. "
                "Find yours via @userinfobot."
            ) from exc
        if parsed <= 0:
            raise ValueError(f"Telegram user IDs must be positive, got {parsed}.")
        ids.add(parsed)
    return tuple(sorted(ids))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        # Aliases (APP_MODE, PORT) are how the *environment* spells them, but
        # code and tests must also be able to use the field name. Without this,
        # Settings(mode=...) is silently ignored as "extra" and the live-mode
        # safety checks below never run.
        populate_by_name=True,
    )

    # --- service ------------------------------------------------------------
    app_name: str = "auto-manager"
    app_version: str = "0.1.0"
    mode: AppMode = Field(default=AppMode.SHADOW, alias="APP_MODE")
    log_level: str = "info"
    host: str = "0.0.0.0"
    port: int = Field(default=10000, alias="PORT")

    # --- database -----------------------------------------------------------
    database_url: SecretStr | None = None
    db_search_path: str = "app, public"
    db_ssl: str = "prefer"
    db_pool_min: int = Field(default=1, ge=0, le=50)
    db_pool_max: int = Field(default=5, ge=1, le=100)
    db_connect_timeout: float = Field(default=10.0, gt=0, le=120)
    db_query_timeout: float = Field(default=15.0, gt=0, le=600)

    # --- control plane ------------------------------------------------------
    control_token: SecretStr | None = None

    # --- telegram user client ----------------------------------------------
    telegram_api_id: int | None = Field(default=None, gt=0)
    telegram_api_hash: SecretStr | None = None
    telegram_session_string: SecretStr | None = None
    telegram_owner_user_ids: tuple[int, ...] = ()
    telegram_main_admin_user_id: int | None = Field(default=None, gt=0)

    # --- worker / queue -----------------------------------------------------
    worker_enabled: bool = True
    worker_poll_seconds: float = Field(default=2.0, ge=0.1, le=600)
    worker_error_backoff: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0, 60.0)
    claim_lease_seconds: int = Field(default=120, ge=5, le=3600)
    job_timeout_seconds: float = Field(default=900.0, ge=1, le=7200)
    graceful_shutdown_seconds: float = Field(default=25.0, ge=0.1, le=120)
    reconcile_on_boot: bool = True
    # Applies supabase/migrations on first boot when the schema is absent, so a
    # deployment needs no manual SQL step. Only runs when app.job does not
    # exist, and the SQL itself is idempotent.
    migrate_on_boot: bool = True
    # Run the read-only protocol probe (app/probe.py) once after boot and DM the
    # report to the owner. Exists because protocol discovery cannot happen from a
    # development machine whose network filters Telegram, only from the deployed
    # service. Off by default: it talks to two bots on the operator's account.
    probe_on_boot: bool = False
    campaign_rate_per_hour: int = Field(default=20, ge=1, le=500)

    # ------------------------------------------------------------------ validators
    @field_validator("mode", mode="before")
    @classmethod
    def _normalise_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip().lower()
            if cleaned not in {m.value for m in AppMode}:
                raise ValueError(
                    f"APP_MODE must be 'shadow' or 'live', got {value!r}."
                )
            return cleaned
        return value

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_database_url(cls, value: Any) -> Any:
        if value is None:
            return None
        text = str(value).strip().strip('"').strip("'")
        if not text:
            return None
        if text.startswith("postgres://"):
            # asyncpg rejects the legacy scheme.
            text = "postgresql://" + text[len("postgres://") :]
        if not text.startswith("postgresql://") and not text.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must be a postgresql:// URI. Use the Supabase "
                "connection-pooler string shown in Project settings → Database."
            )
        return text

    @field_validator("worker_error_backoff", mode="before")
    @classmethod
    def _parse_backoff(cls, value: Any) -> Any:
        """Accept ``2,5,10,30,60`` from the environment."""
        if value is None or isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            parts = [p.strip() for p in re.split(r"[,;\s]+", value.strip().strip("[]")) if p.strip()]
            try:
                parsed = tuple(float(p) for p in parts)
            except ValueError as exc:
                raise ValueError(
                    "WORKER_ERROR_BACKOFF must be comma-separated seconds, e.g. 2,5,10,30,60"
                ) from exc
            if not parsed:
                return (2.0, 5.0, 10.0, 30.0, 60.0)
            if min(parsed) < 0.05:
                raise ValueError("WORKER_ERROR_BACKOFF entries must be at least 0.05 seconds")
            return parsed
        return value

    @field_validator("telegram_owner_user_ids", mode="before")
    @classmethod
    def _parse_owner_ids(cls, value: Any) -> tuple[int, ...]:
        return _parse_user_ids(value)

    @field_validator("db_ssl")
    @classmethod
    def _check_ssl_mode(cls, value: str) -> str:
        allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        if value not in allowed:
            raise ValueError(f"DB_SSL must be one of {sorted(allowed)}")
        return value

    @model_validator(mode="after")
    def _enforce_live_guards(self) -> "Settings":
        if self.mode is not AppMode.LIVE:
            return self
        missing: list[str] = []
        if self.database_url is None:
            missing.append("DATABASE_URL (Supabase pooler connection string)")
        if self.control_token is None:
            missing.append("CONTROL_TOKEN (protects /control/pause and /shutdown)")
        if self.telegram_api_id is None:
            missing.append("TELEGRAM_API_ID (from my.telegram.org)")
        if self.telegram_api_hash is None:
            missing.append("TELEGRAM_API_HASH")
        if self.telegram_session_string is None:
            missing.append("TELEGRAM_SESSION_STRING (from scripts/login.py)")
        if not self.telegram_owner_user_ids:
            missing.append("TELEGRAM_OWNER_USER_IDS (who may issue owner commands)")
        if missing:
            raise ValueError(
                "APP_MODE=live refuses to start until these are set in the "
                "deployment environment:\n  - "
                + "\n  - ".join(missing)
            )
        return self

    # ------------------------------------------------------------------ helpers
    def boot_audit(self) -> list[str]:
        """Human-readable startup report, used to diagnose a misconfigured deploy.

        Never echoes a secret *value*. Each line names what is missing and what
        that means, because the first real Render deploy reported "live 🎉" while
        persisting nothing — the connection string had never been saved, and the
        only clue was a one-word warning. This is the text an operator pastes.
        """
        lines: list[str] = []

        if self.database_url is not None:
            host = urlparse(self.database_url.get_secret_value()).hostname or "unparseable"
            lines.append(f"DATABASE_URL set (host {host}, ssl={self.db_ssl})")
        else:
            lines.append(
                "DATABASE_URL is NOT set: no persistence, no queue, nothing gets "
                "processed. Render -> your service -> Environment -> add DATABASE_URL "
                "with the Supabase session-pooler connection string -> Saved Changes"
            )

        lines.append(
            "CONTROL_TOKEN set"
            if self.control_token is not None
            else "CONTROL_TOKEN is NOT set: the /control kill switch and manual reconcile stay disabled"
        )
        lines.append(
            f"APP_MODE={self.mode.value}"
            + ("" if self.mode is AppMode.LIVE else " (no Telegram sending)")
        )

        missing = [
            name
            for name, value in (
                ("TELEGRAM_API_ID", self.telegram_api_id),
                ("TELEGRAM_API_HASH", self.telegram_api_hash),
                ("TELEGRAM_SESSION_STRING", self.telegram_session_string),
            )
            if value is None
        ]
        lines.append(
            "Telegram client configured"
            if not missing
            else "Telegram client not configured yet: " + ", ".join(missing)
        )
        return lines

    @property
    def uses_transaction_pooler(self) -> bool:
        """True for Supabase's port-6543 pooler, which needs prepared
        statements disabled or every query fails behind the pooler."""
        url = self.database_url.get_secret_value() if self.database_url else ""
        return ":6543" in url or "pooler.supabase.com" in url

    @property
    def outbound_enabled(self) -> bool:
        """Whether Telegram side-effects are permitted at all."""
        return (
            self.mode is AppMode.LIVE
            and self.telegram_session_string is not None
            and self.telegram_api_id is not None
            and self.telegram_api_hash is not None
        )

    @property
    def owner_ids(self) -> frozenset[int]:
        ids = set(self.telegram_owner_user_ids)
        if self.telegram_main_admin_user_id:
            ids.add(self.telegram_main_admin_user_id)
        return frozenset(ids)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.owner_ids

    def reveal(self, field_name: str) -> str | None:
        """Deliberately awkward to call: reading a secret requires naming it."""
        value = getattr(self, field_name, None)
        return value.get_secret_value() if isinstance(value, SecretStr) else value

    def safe_dump(self) -> dict[str, Any]:
        """Everything except secret values — safe for logs and /status."""
        data: dict[str, Any] = {}
        for name, value in self.model_dump(mode="json").items():
            if name in SENSITIVE_FIELDS:
                configured = getattr(self, name, None) is not None
                data[name] = "configured" if configured else "MISSING"
            else:
                data[name] = value
        data["outbound_enabled"] = self.outbound_enabled
        return data


def load_settings(**overrides: Any) -> Settings:
    """Build settings, tolerating an absent ``.env`` (Render has no .env file)."""
    kwargs: dict[str, Any] = {k: v for k, v in overrides.items() if v is not _MISSING}
    return Settings(**kwargs)
