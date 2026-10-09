"""Unit tests for configuration validation and overrides."""

import pytest
from pydantic import SecretStr, ValidationError

from meta_bot.config import Settings, get_settings, set_settings_override


def test_valid_settings(valid_fernet_key: str) -> None:
    """Test valid configuration instantiated cleanly."""
    settings = Settings(
        APP_ID="12345",
        APP_SECRET=SecretStr("secret123"),
        VERIFY_TOKEN=SecretStr("verify123"),
        PAGE_ID="page123",
        IG_ACCOUNT_ID="ig123",
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
        REDIS_URL="redis://localhost:6379/0",
        TOKEN_ENC_KEY=SecretStr(valid_fernet_key),
        ADMIN_TOKEN=SecretStr("admin123"),
    )
    assert settings.APP_ID == "12345"
    assert settings.MAX_PRIVATE_PER_HOUR == 100
    assert settings.GRAPH_VERSION == "v25.0"


@pytest.mark.parametrize("invalid_hourly", [0, -10, 751, 1000])
def test_invalid_max_private_per_hour(
    valid_fernet_key: str, invalid_hourly: int
) -> None:
    """Meta rule: max private replies ceiling is 750."""
    with pytest.raises(ValidationError, match="MAX_PRIVATE_PER_HOUR"):
        Settings(
            APP_ID="12345",
            APP_SECRET=SecretStr("secret123"),
            VERIFY_TOKEN=SecretStr("verify123"),
            PAGE_ID="page123",
            IG_ACCOUNT_ID="ig123",
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
            REDIS_URL="redis://localhost:6379/0",
            TOKEN_ENC_KEY=SecretStr(valid_fernet_key),
            ADMIN_TOKEN=SecretStr("admin123"),
            MAX_PRIVATE_PER_HOUR=invalid_hourly,
        )


def test_invalid_daily_cap_less_than_hourly(valid_fernet_key: str) -> None:
    """Daily cap cannot be less than hourly cap."""
    with pytest.raises(ValidationError, match="MAX_PRIVATE_PER_DAY"):
        Settings(
            APP_ID="12345",
            APP_SECRET=SecretStr("secret123"),
            VERIFY_TOKEN=SecretStr("verify123"),
            PAGE_ID="page123",
            IG_ACCOUNT_ID="ig123",
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
            REDIS_URL="redis://localhost:6379/0",
            TOKEN_ENC_KEY=SecretStr(valid_fernet_key),
            ADMIN_TOKEN=SecretStr("admin123"),
            MAX_PRIVATE_PER_HOUR=100,
            MAX_PRIVATE_PER_DAY=50,
        )


def test_invalid_send_delay_range(valid_fernet_key: str) -> None:
    """Min delay must not exceed max delay."""
    with pytest.raises(ValidationError, match="SEND_DELAY_MIN_SECONDS"):
        Settings(
            APP_ID="12345",
            APP_SECRET=SecretStr("secret123"),
            VERIFY_TOKEN=SecretStr("verify123"),
            PAGE_ID="page123",
            IG_ACCOUNT_ID="ig123",
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
            REDIS_URL="redis://localhost:6379/0",
            TOKEN_ENC_KEY=SecretStr(valid_fernet_key),
            ADMIN_TOKEN=SecretStr("admin123"),
            SEND_DELAY_MIN_SECONDS=60,
            SEND_DELAY_MAX_SECONDS=20,
        )


@pytest.mark.parametrize("invalid_ratio", [-0.1, 1.5])
def test_invalid_public_reply_ratio(
    valid_fernet_key: str, invalid_ratio: float
) -> None:
    """Ratio must be bounded between 0.0 and 1.0."""
    with pytest.raises(ValidationError, match="PUBLIC_REPLY_RATIO"):
        Settings(
            APP_ID="12345",
            APP_SECRET=SecretStr("secret123"),
            VERIFY_TOKEN=SecretStr("verify123"),
            PAGE_ID="page123",
            IG_ACCOUNT_ID="ig123",
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
            REDIS_URL="redis://localhost:6379/0",
            TOKEN_ENC_KEY=SecretStr(valid_fernet_key),
            ADMIN_TOKEN=SecretStr("admin123"),
            PUBLIC_REPLY_RATIO=invalid_ratio,
        )


def test_invalid_fernet_key() -> None:
    """TOKEN_ENC_KEY must be a valid Fernet key."""
    with pytest.raises(ValidationError, match="TOKEN_ENC_KEY"):
        Settings(
            APP_ID="12345",
            APP_SECRET=SecretStr("secret123"),
            VERIFY_TOKEN=SecretStr("verify123"),
            PAGE_ID="page123",
            IG_ACCOUNT_ID="ig123",
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
            REDIS_URL="redis://localhost:6379/0",
            TOKEN_ENC_KEY=SecretStr("not-a-valid-fernet-key"),
            ADMIN_TOKEN=SecretStr("admin123"),
        )


def test_invalid_graph_version(valid_fernet_key: str) -> None:
    """GRAPH_VERSION must start with 'v'."""
    with pytest.raises(ValidationError, match="GRAPH_VERSION"):
        Settings(
            APP_ID="12345",
            APP_SECRET=SecretStr("secret123"),
            VERIFY_TOKEN=SecretStr("verify123"),
            PAGE_ID="page123",
            IG_ACCOUNT_ID="ig123",
            DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db",
            REDIS_URL="redis://localhost:6379/0",
            TOKEN_ENC_KEY=SecretStr(valid_fernet_key),
            ADMIN_TOKEN=SecretStr("admin123"),
            GRAPH_VERSION="25.0",
        )


def test_settings_override(test_settings: Settings) -> None:
    """Test get_settings injection and override mechanism."""
    assert get_settings() == test_settings
    set_settings_override(None)
