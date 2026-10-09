"""Configuration management for Meta comment and DM automation bot."""

from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file.

    All configurations are frozen to ensure immutability at runtime.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Runtime Environment
    APP_ENV: Literal["local", "staging", "production"] = "local"
    LOG_LEVEL: str = "INFO"

    # Meta App Credentials
    APP_ID: str = Field(description="Meta Facebook App ID")
    APP_SECRET: SecretStr = Field(description="Meta Facebook App Secret")
    VERIFY_TOKEN: SecretStr = Field(description="Webhook verification token")

    # Meta Graph API
    # TODO(verify): Confirm the active supported Graph API version in Meta developer docs
    GRAPH_VERSION: str = Field(
        default="v25.0",
        description="Pinned Meta Graph API version prefix",
    )
    GRAPH_BASE_URL: str = Field(
        default="https://graph.facebook.com",
        description="Base URL for Meta Graph API calls",
    )

    # Meta Assets
    PAGE_ID: str = Field(description="Facebook Page ID")
    IG_ACCOUNT_ID: str = Field(
        description="Connected Instagram Professional Account ID"
    )
    PAGE_ACCESS_TOKEN: SecretStr | None = Field(
        default=None,
        description="Page access token; optional at boot for local environment",
    )

    # Databases & Queues
    DATABASE_URL: str = Field(description="PostgreSQL async connection URL")
    REDIS_URL: str = Field(description="Redis connection URL")

    # Security & Administration
    TOKEN_ENC_KEY: SecretStr = Field(
        description="Fernet symmetric key (32 url-safe base64 bytes) for token encryption at rest",
    )
    ADMIN_TOKEN: SecretStr = Field(
        description="Bearer token for internal admin endpoints",
    )

    # Rate Limiting & Pacing
    # Meta hard ceiling: 750 private replies/hour per Instagram account.
    MAX_PRIVATE_PER_HOUR: int = Field(
        default=100,
        description="Hourly cap for private replies (must be between 1 and 750)",
    )
    MAX_PRIVATE_PER_DAY: int = Field(
        default=1000,
        description="Daily cap for private replies",
    )
    SEND_DELAY_MIN_SECONDS: int = Field(
        default=20,
        description="Minimum pacing delay in seconds before sending first DM",
    )
    SEND_DELAY_MAX_SECONDS: int = Field(
        default=60,
        description="Maximum pacing delay in seconds before sending first DM",
    )
    PUBLIC_REPLY_RATIO: float = Field(
        default=0.3,
        description="Fraction of comments to reply publicly (0.0 to 1.0)",
    )

    # Meta Policy Windows
    DM_WINDOW_HOURS: int = Field(
        default=24,
        description="Standard 24-hour customer service messaging window",
    )
    PRIVATE_REPLY_WINDOW_DAYS: int = Field(
        default=7,
        description="Meta rule: maximum 7 days to send a private reply to a comment",
    )

    # Bot Fallbacks & LLM Budgets
    # Meta rule: Bots must answer every user input within 30 seconds.
    BOT_REPLY_BUDGET_SECONDS: int = Field(
        default=20,
        description="Max total seconds for bot reply pipeline before fast fallback triggers",
    )
    LLM_TIMEOUT_SECONDS: int = Field(
        default=12,
        description="Timeout for external LLM inference before falling back to fixed response",
    )

    # Operational Flags
    ALERT_WEBHOOK_URL: str | None = Field(
        default=None,
        description="Webhook URL for high-priority operational alert dispatching",
    )
    KILL_SWITCH_DEFAULT: bool = Field(
        default=False,
        description="Default kill switch state at system startup",
    )
    DEMO_MODE: bool = Field(
        default=False,
        description="When true, write calls to Meta are logged instead of executed",
    )
    ALLOW_REPLIES_TO_REPLIES: bool = Field(
        default=False,
        description="Whether threaded comment replies trigger automated private replies",
    )
    MAX_IDENTICAL_PUBLIC_PER_POST: int = Field(
        default=3,
        description="Maximum times an identical public reply text may be posted to the same post",
    )
    CIRCUIT_BREAKER_ERROR_THRESHOLD: int = Field(
        default=5,
        description="Number of consecutive critical Meta errors before tripping circuit breaker",
    )
    CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS: int = Field(
        default=3600,
        description="Cooldown duration before circuit breaker allows reset",
    )
    DEFAULT_HUMAN_CONTACT: str = Field(
        default="support@example.com",
        description="Contact information for human agent escalation",
    )

    @field_validator("MAX_PRIVATE_PER_HOUR")
    @classmethod
    def validate_hourly_cap(cls, v: int) -> int:
        """Enforce Meta's ceiling of 750 private replies per hour."""
        if not (1 <= v <= 750):
            raise ValueError(
                f"MAX_PRIVATE_PER_HOUR must be between 1 and 750 (Meta ceiling), got {v}"
            )
        return v

    @field_validator("PUBLIC_REPLY_RATIO")
    @classmethod
    def validate_reply_ratio(cls, v: float) -> float:
        """Enforce public reply ratio within [0.0, 1.0]."""
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"PUBLIC_REPLY_RATIO must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("TOKEN_ENC_KEY")
    @classmethod
    def validate_fernet_key(cls, v: SecretStr) -> SecretStr:
        """Ensure token encryption key is a valid Fernet key."""
        try:
            Fernet(v.get_secret_value().encode("utf-8"))
        except Exception as exc:
            raise ValueError(
                f"TOKEN_ENC_KEY must be a valid 32 url-safe base64-encoded Fernet key: {exc}"
            ) from exc
        return v

    @field_validator("GRAPH_VERSION")
    @classmethod
    def validate_graph_version(cls, v: str) -> str:
        """Ensure GRAPH_VERSION starts with 'v' followed by digits."""
        v_stripped = v.strip()
        if not v_stripped.startswith("v"):
            raise ValueError(
                f"GRAPH_VERSION must start with 'v' (e.g. 'v25.0'), got '{v}'"
            )
        return v_stripped

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> "Settings":
        """Validate dependencies and relationships between configuration fields."""
        if self.SEND_DELAY_MIN_SECONDS < 0:
            raise ValueError("SEND_DELAY_MIN_SECONDS must be non-negative")
        if self.SEND_DELAY_MIN_SECONDS > self.SEND_DELAY_MAX_SECONDS:
            raise ValueError(
                f"SEND_DELAY_MIN_SECONDS ({self.SEND_DELAY_MIN_SECONDS}) cannot exceed "
                f"SEND_DELAY_MAX_SECONDS ({self.SEND_DELAY_MAX_SECONDS})"
            )
        if self.MAX_PRIVATE_PER_DAY < self.MAX_PRIVATE_PER_HOUR:
            raise ValueError(
                f"MAX_PRIVATE_PER_DAY ({self.MAX_PRIVATE_PER_DAY}) cannot be less than "
                f"MAX_PRIVATE_PER_HOUR ({self.MAX_PRIVATE_PER_HOUR})"
            )
        return self


_SETTINGS_OVERRIDE: Settings | None = None


def set_settings_override(settings: Settings | None) -> None:
    """Inject a custom settings instance for testing."""
    global _SETTINGS_OVERRIDE
    _SETTINGS_OVERRIDE = settings
    get_settings.cache_clear()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve cached application settings instance."""
    if _SETTINGS_OVERRIDE is not None:
        return _SETTINGS_OVERRIDE
    return Settings()  # type: ignore[call-arg]
