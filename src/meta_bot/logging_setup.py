"""Structured logging setup with correlation ID tracing and sensitive data redaction."""

import logging
from collections.abc import MutableMapping
from contextvars import ContextVar, Token
from typing import Any

import structlog
from pydantic import SecretStr

# Context variable storing the current request or event correlation ID
correlation_id_ctx: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Keys that must always be masked to prevent leaking secrets or customer PII
SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "authorization",
    "access_token",
    "app_secret",
    "verify_token",
    "admin_token",
    "enc_key",
    "api_key",
    "private_key",
)

REDACTED_PLACEHOLDER = "[REDACTED]"


def get_correlation_id() -> str | None:
    """Retrieve current correlation ID from context."""
    return correlation_id_ctx.get()


def set_correlation_id(correlation_id: str | None) -> Token[str | None]:
    """Set correlation ID for current async context."""
    return correlation_id_ctx.set(correlation_id)


def _sanitize_value(key: str, value: Any) -> Any:
    """Recursively redact sensitive keys, SecretStr instances, and token-like values."""
    if isinstance(value, SecretStr):
        return REDACTED_PLACEHOLDER

    lowered_key = key.lower()
    if any(sub in lowered_key for sub in SENSITIVE_KEY_SUBSTRINGS):
        return REDACTED_PLACEHOLDER

    if isinstance(value, dict):
        return {k: _sanitize_value(str(k), v) for k, v in value.items()}

    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(key, item) for item in value)
    if isinstance(value, set):
        return {_sanitize_value(key, item) for item in value}

    if isinstance(value, str) and "bearer " in value.lower():
        return REDACTED_PLACEHOLDER

    return value


def redaction_processor(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor that sanitizes tokens, passwords, and sensitive keys."""
    sanitized: dict[str, Any] = {}
    for k, v in event_dict.items():
        sanitized[k] = _sanitize_value(k, v)
    event_dict.clear()
    event_dict.update(sanitized)
    return event_dict


def correlation_id_processor(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor that attaches correlation_id to log event."""
    cid = get_correlation_id()
    if cid is not None and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    return event_dict


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog for JSON formatted output with redaction and context tracking."""
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        correlation_id_processor,
        redaction_processor,
        structlog.processors.JSONRenderer(),
    ]

    level_num: int = int(getattr(logging, log_level.upper(), logging.INFO))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
