from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AppMode, Settings


def test_defaults_are_shadow_mode(make_settings) -> None:
    settings = make_settings()
    assert settings.mode is AppMode.SHADOW
    # Shadow mode must not be able to touch anyone's Telegram.
    assert settings.outbound_enabled is False


def test_live_mode_refuses_to_start_without_guards(make_settings) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_settings(mode=AppMode.LIVE)
    message = str(excinfo.value)
    for expected in ("DATABASE_URL", "CONTROL_TOKEN", "TELEGRAM_SESSION_STRING", "TELEGRAM_OWNER_USER_IDS"):
        assert expected in message, f"{expected} should be named in the error"


def test_live_mode_starts_when_everything_is_present(make_settings) -> None:
    settings = make_settings(
        mode=AppMode.LIVE,
        database_url="postgresql://u:p@host:6543/db",
        control_token="x" * 40,
        telegram_api_id=12345,
        telegram_api_hash="0123456789abcdef0123456789abcdef",  # allowlist: secret
        telegram_session_string="1AAAAAAAvfy4ycE...",
        telegram_owner_user_ids=[111],
    )
    assert settings.outbound_enabled is True


def test_secrets_never_appear_in_safe_dump(make_settings) -> None:
    password = "SUuP3R-s3cr3t-value"
    settings = make_settings(database_url=f"postgresql://postgres:{password}@host/db")  # allowlist: secret
    dumped = settings.safe_dump()
    assert password not in str(dumped)
    assert dumped["database_url"] == "configured"
    assert "postgres://" not in str(dumped)


def test_missing_secret_reports_missing_not_none(make_settings) -> None:
    assert make_settings().safe_dump()["control_token"] == "MISSING"


def test_legacy_postgres_scheme_is_rewritten(make_settings) -> None:
    """asyncpg rejects postgres://, which is exactly what most tutorials print."""
    settings = make_settings(database_url="postgres://u:p@host/db")
    assert settings.reveal("database_url").startswith("postgresql://")


def test_pooler_detection_disables_statement_cache(make_settings) -> None:
    assert make_settings(database_url="postgresql://u:p@pooler:6543/db").uses_transaction_pooler is True
    assert make_settings(database_url="postgresql://u:p@localhost:5432/db").uses_transaction_pooler is False


def test_garbage_database_url_is_rejected(make_settings) -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url="supabase://nope")


def test_owner_ids_accept_comma_lists_and_json(make_settings) -> None:
    assert make_settings(telegram_owner_user_ids="42, 7").telegram_owner_user_ids == (7, 42)
    assert make_settings(telegram_owner_user_ids="[9]").telegram_owner_user_ids == (9,)
    assert make_settings(telegram_owner_user_ids="").telegram_owner_user_ids == ()


def test_non_numeric_owner_id_is_rejected_with_guidance(make_settings) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_settings(telegram_owner_user_ids="@myuser")
    assert "userinfobot" in str(excinfo.value)


def test_main_admin_is_always_an_owner(make_settings) -> None:
    settings = make_settings(telegram_owner_user_ids=[5], telegram_main_admin_user_id=9)
    assert settings.is_owner(9) and settings.is_owner(5)
    assert not settings.is_owner(6)
    assert not settings.is_owner(None)


def test_invalid_app_mode_message(make_settings) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_settings(mode="yolo")
    assert "shadow" in str(excinfo.value)


def test_blank_env_values_are_treated_as_unset(make_settings) -> None:
    """Render writes an empty value for an unfilled secret field."""
    settings = make_settings(database_url="   ", control_token="")
    assert settings.database_url is None


def test_numeric_bounds_are_enforced(make_settings) -> None:
    with pytest.raises(ValidationError):
        make_settings(claim_lease_seconds=2)
    with pytest.raises(ValidationError):
        make_settings(campaign_rate_per_hour=100_000)


def test_settings_repr_does_not_leak(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_TOKEN", "leak-me-please-0123456789")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")
    settings = Settings(_env_file=None)
    assert "leak-me-please" not in repr(settings)
    assert "0123456789abcdef" not in repr(settings)


class TestBootAudit:
    """A misconfigured deploy has to say so in one line instead of idling.

    The first real Render deploy came up "live 🎉" while persisting nothing,
    because the connection string was never saved; these lines are what the
    operator then pastes into chat, so they must name the cause and the fix.
    """

    def test_missing_database_url_is_named_as_the_cause(self) -> None:
        notes = Settings(app_name="auto-manager", database_url=None, control_token=None).boot_audit()
        problem = next(n for n in notes if n.startswith("DATABASE_URL"))
        assert "NOT set" in problem
        assert "no persistence" in problem and "Environment" in problem

    def test_configured_deploy_reports_no_problems(self) -> None:
        settings = Settings(
            app_name="auto-manager",
            database_url="postgresql://postgres:<pw>@host:5432/postgres",
            control_token="three random words",
        )
        notes = settings.boot_audit()
        assert not any("NOT set" in n for n in notes)
        assert any("host host" in n for n in notes)

    def test_audit_never_echoes_a_secret_value(self) -> None:
        settings = Settings(
            app_name="auto-manager",
            database_url="postgresql://postgres:<the-password>@db.example:5432/postgres",
            control_token="<the-kill-switch-phrase>",
            telegram_api_hash="<the-api-hash>",
            telegram_session_string="<the-session>",
        )
        joined = " | ".join(settings.boot_audit())
        for secret in ("the-password", "the-kill-switch-phrase", "the-api-hash", "the-session"):
            assert secret not in joined
        assert "db.example" in joined  # host is useful and safe to show

    def test_shadow_mode_is_reported_as_not_sending(self) -> None:
        notes = Settings(app_name="auto-manager", mode="shadow").boot_audit()
        assert any("no Telegram sending" in n for n in notes)

    def test_missing_telegram_fields_are_listed(self) -> None:
        # Shadow mode: the audit still lists what is absent, because the operator
        # needs to know before flipping the mode and wondering why nothing sent.
        notes = Settings(app_name="auto-manager").boot_audit()
        listed = next(n for n in notes if n.startswith("Telegram client"))
        assert "TELEGRAM_API_ID" in listed and "TELEGRAM_SESSION_STRING" in listed
