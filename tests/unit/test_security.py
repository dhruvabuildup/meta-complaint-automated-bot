"""Unit tests for webhook cryptographic security helpers."""

import base64
import hashlib
import hmac
import json

from meta_bot.api.security import (
    parse_signed_request,
    verify_signature,
    verify_webhook_challenge,
)


def _compute_sig(body: bytes, secret: str) -> str:
    """Helper to compute valid sha256 header."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def test_verify_signature_valid() -> None:
    """Test valid signature passes."""
    secret = "my_app_secret_123"
    body = b'{"object": "instagram", "entry": []}'
    header = _compute_sig(body, secret)
    assert verify_signature(body, header, secret) is True


def test_verify_signature_tampered_body() -> None:
    """Test tampered body with original signature is rejected."""
    secret = "my_app_secret_123"
    body = b'{"object": "instagram"}'
    header = _compute_sig(body, secret)
    tampered_body = b'{"object": "instagram", "hacked": true}'
    assert verify_signature(tampered_body, header, secret) is False


def test_verify_signature_wrong_secret() -> None:
    """Test signature computed with wrong secret is rejected."""
    body = b'{"test": "payload"}'
    header = _compute_sig(body, "wrong_secret")
    assert verify_signature(body, header, "actual_secret") is False


def test_verify_signature_missing_header() -> None:
    """Test missing or empty header is rejected."""
    assert verify_signature(b"test", None, "secret") is False
    assert verify_signature(b"test", "", "secret") is False


def test_verify_signature_malformed_header() -> None:
    """Test malformed header format is rejected."""
    secret = "secret"
    body = b"test"
    # Missing 'sha256=' prefix
    assert verify_signature(body, "1234567890abcdef", secret) is False
    # Empty signature after prefix
    assert verify_signature(body, "sha256=", secret) is False


def test_verify_webhook_challenge_success() -> None:
    """Test successful handshake challenge verification."""
    verify_token = "my_verify_token_999"
    challenge = "random_challenge_string_12345"
    result = verify_webhook_challenge(
        mode="subscribe",
        token=verify_token,
        challenge=challenge,
        verify_token=verify_token,
    )
    assert result == challenge


def test_verify_webhook_challenge_invalid_mode() -> None:
    """Test rejection when hub.mode is not 'subscribe'."""
    assert (
        verify_webhook_challenge(
            mode="unsubscribe",
            token="token",
            challenge="123",
            verify_token="token",
        )
        is None
    )


def test_verify_webhook_challenge_invalid_token() -> None:
    """Test rejection when hub.verify_token does not match."""
    assert (
        verify_webhook_challenge(
            mode="subscribe",
            token="wrong_token",
            challenge="123",
            verify_token="expected_token",
        )
        is None
    )


def test_parse_signed_request_valid() -> None:
    """Test parsing and verifying valid signed_request."""
    secret = "test_app_secret"
    payload = {"algorithm": "HMAC-SHA256", "user_id": "12345", "issued_at": 1600000000}
    payload_json = json.dumps(payload).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload_json).decode("utf-8").rstrip("=")

    expected_sig = hmac.new(
        secret.encode(), encoded_payload.encode(), hashlib.sha256
    ).digest()
    encoded_sig = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

    signed_request = f"{encoded_sig}.{encoded_payload}"
    parsed = parse_signed_request(signed_request, secret)
    assert parsed == payload


def test_parse_signed_request_tampered() -> None:
    """Test parsing tampered signed_request returns None."""
    secret = "test_app_secret"
    signed_request = "invalid_sig.invalid_payload"
    assert parse_signed_request(signed_request, secret) is None
