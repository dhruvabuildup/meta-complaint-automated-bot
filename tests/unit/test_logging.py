"""Unit tests for structlog logging and redaction processors."""

from pydantic import SecretStr

from meta_bot.logging_setup import (
    REDACTED_PLACEHOLDER,
    get_correlation_id,
    redaction_processor,
    set_correlation_id,
)


def test_redaction_processor_masks_sensitive_keys() -> None:
    """Ensure sensitive dictionary keys are redacted regardless of casing."""
    event = {
        "event": "user_action",
        "access_token": "EAAB1234567890",
        "app_secret": "my_super_secret",
        "verify_token": "verify_value",
        "admin_token": "admin_value",
        "safe_id": "12345",
    }
    processed = redaction_processor(None, "info", event)

    assert processed["safe_id"] == "12345"
    assert processed["access_token"] == REDACTED_PLACEHOLDER
    assert processed["app_secret"] == REDACTED_PLACEHOLDER
    assert processed["verify_token"] == REDACTED_PLACEHOLDER
    assert processed["admin_token"] == REDACTED_PLACEHOLDER


def test_redaction_processor_masks_secret_str() -> None:
    """Ensure SecretStr instances are masked even if key is not explicitly sensitive."""
    event = {
        "custom_value": SecretStr("sensitive_payload"),
    }
    processed = redaction_processor(None, "info", event)
    assert processed["custom_value"] == REDACTED_PLACEHOLDER


def test_redaction_processor_masks_nested_data() -> None:
    """Ensure nested dictionaries and lists are recursively scrubbed."""
    event = {
        "data": {
            "sub_token": "12345",
            "nested_list": [{"password": "pass"}, "safe_item"],
        }
    }
    processed = redaction_processor(None, "info", event)
    assert processed["data"]["sub_token"] == REDACTED_PLACEHOLDER
    assert processed["data"]["nested_list"][0]["password"] == REDACTED_PLACEHOLDER
    assert processed["data"]["nested_list"][1] == "safe_item"


def test_redaction_processor_masks_bearer_tokens() -> None:
    """Ensure bearer strings within arbitrary values are masked."""
    event = {
        "header_echo": "Bearer abcdef123456789",
    }
    processed = redaction_processor(None, "info", event)
    assert processed["header_echo"] == REDACTED_PLACEHOLDER


def test_correlation_id_context_lifecycle() -> None:
    """Test setting and retrieving correlation ID via contextvar."""
    assert get_correlation_id() is None
    set_correlation_id("test-corr-id-123")
    assert get_correlation_id() == "test-corr-id-123"
    set_correlation_id(None)
    assert get_correlation_id() is None
