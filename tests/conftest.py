"""Pytest fixtures for unit and integration tests."""

import os
from collections.abc import AsyncGenerator, Generator

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from meta_bot.config import Settings, set_settings_override


@pytest.fixture(scope="session")
def valid_fernet_key() -> str:
    """Generate a valid Fernet key for tests."""
    return Fernet.generate_key().decode()


@pytest.fixture
def test_settings(valid_fernet_key: str) -> Generator[Settings, None, None]:
    """Provide isolated Settings instance for test execution."""
    settings = Settings(
        APP_ENV="local",
        LOG_LEVEL="DEBUG",
        APP_ID="123456789012345",
        APP_SECRET=SecretStr("mock_app_secret_value_for_testing"),
        VERIFY_TOKEN=SecretStr("mock_webhook_verify_token_value"),
        GRAPH_VERSION="v25.0",
        GRAPH_BASE_URL="https://graph.facebook.com",
        PAGE_ID="100000000000001",
        IG_ACCOUNT_ID="17841400000000001",
        PAGE_ACCESS_TOKEN=SecretStr("mock_page_access_token_testing"),
        DATABASE_URL=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgrespassword@localhost:5432/meta_bot_test",
        ),
        REDIS_URL=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        TOKEN_ENC_KEY=SecretStr(valid_fernet_key),
        ADMIN_TOKEN=SecretStr("mock_admin_token_testing_value"),
        MAX_PRIVATE_PER_HOUR=100,
        MAX_PRIVATE_PER_DAY=1000,
        SEND_DELAY_MIN_SECONDS=20,
        SEND_DELAY_MAX_SECONDS=60,
        PUBLIC_REPLY_RATIO=0.3,
        DM_WINDOW_HOURS=24,
        PRIVATE_REPLY_WINDOW_DAYS=7,
        BOT_REPLY_BUDGET_SECONDS=20,
        LLM_TIMEOUT_SECONDS=12,
        KILL_SWITCH_DEFAULT=False,
        DEMO_MODE=False,
    )
    set_settings_override(settings)
    yield settings
    set_settings_override(None)


@pytest.fixture
async def fake_redis() -> AsyncGenerator[fakeredis.aioredis.FakeRedis, None]:
    """Provide an in-memory FakeRedis instance for testing without external I/O."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()
