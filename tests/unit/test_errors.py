"""Unit tests for Meta error hierarchy and mapping."""

import json
from pathlib import Path

import pytest

from meta_bot.errors import (
    MetaAuthError,
    MetaPermissionError,
    MetaRateLimitError,
    MetaTransientError,
    MetaWindowError,
    map_meta_error,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def test_map_auth_error_from_fixture() -> None:
    """Test mapping authentication error fixture to MetaAuthError."""
    with open(FIXTURES_DIR / "meta_error_auth.json") as f:
        payload = json.load(f)

    err = map_meta_error(payload, status_code=400)
    assert isinstance(err, MetaAuthError)
    assert err.meta_code == 190
    assert err.meta_subcode == 463
    assert err.fbtrace_id == "G8vNkm9124c"


def test_map_rate_limit_error_from_fixture() -> None:
    """Test mapping rate limit error fixture to MetaRateLimitError."""
    with open(FIXTURES_DIR / "meta_error_rate_limit.json") as f:
        payload = json.load(f)

    err = map_meta_error(payload, status_code=400, retry_after=30.0)
    assert isinstance(err, MetaRateLimitError)
    assert err.meta_code == 613
    assert err.retry_after == 30.0


def test_map_window_error_from_fixture() -> None:
    """Test mapping policy window error fixture to MetaWindowError."""
    with open(FIXTURES_DIR / "meta_error_window.json") as f:
        payload = json.load(f)

    err = map_meta_error(payload, status_code=400)
    assert isinstance(err, MetaWindowError)
    assert err.meta_subcode == 2018001


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_map_server_transient_errors(status_code: int) -> None:
    """HTTP 5xx status codes must map to MetaTransientError."""
    err = map_meta_error(None, status_code=status_code)
    assert isinstance(err, MetaTransientError)
    assert err.status_code == status_code


@pytest.mark.parametrize("code", [10, 200, 283])
def test_map_permission_error_codes(code: int) -> None:
    """Permission codes must map to MetaPermissionError."""
    payload = {"error": {"code": code, "message": "Permission error"}}
    err = map_meta_error(payload, status_code=403)
    assert isinstance(err, MetaPermissionError)


def test_token_redacted_in_error_message() -> None:
    """Access tokens must never leak into error messages."""
    raw_message = "Token EAAB1234567890abcdef is expired. Header: Bearer xyz123456"
    payload = {"error": {"code": 190, "message": raw_message}}
    err = map_meta_error(payload, status_code=400)

    assert "EAAB1234567890abcdef" not in str(err)
    assert "xyz123456" not in str(err)
    assert "[REDACTED_TOKEN]" in str(err)
