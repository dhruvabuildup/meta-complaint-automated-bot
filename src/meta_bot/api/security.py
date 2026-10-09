"""Cryptographic security helpers for Meta webhook signatures and challenges."""

import base64
import hashlib
import hmac
import json
from typing import Any


def verify_signature(
    raw_body: bytes,
    header: str | None,
    app_secret: str,
) -> bool:
    """Verify Meta webhook raw payload against X-Hub-Signature-256 header.

    Meta rule: The header format is 'sha256=<hash>', calculated via HMAC-SHA256
    using the App Secret as key and the raw request body as data.
    Uses constant-time comparison to prevent timing attacks.

    Args:
        raw_body: Unmodified raw request body bytes.
        header: X-Hub-Signature-256 header value.
        app_secret: Meta application secret key.

    Returns:
        True if signature matches, False otherwise.
    """
    if not header or not app_secret:
        return False

    prefix = "sha256="
    if not header.startswith(prefix):
        return False

    expected_signature = header[len(prefix) :].strip()
    if not expected_signature:
        return False

    try:
        computed_hash = hmac.new(
            key=app_secret.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        ).hexdigest()
    except (TypeError, ValueError):
        return False

    return hmac.compare_digest(computed_hash, expected_signature)


def verify_webhook_challenge(
    mode: str | None,
    token: str | None,
    challenge: str | None,
    verify_token: str,
) -> str | None:
    """Validate GET webhook verification handshake request from Meta.

    Meta rule: Meta sends hub.mode='subscribe', hub.verify_token=<token>,
    and hub.challenge=<challenge>. Returns the challenge on match, None on error.
    """
    if mode != "subscribe" or not token or not challenge or not verify_token:
        return None

    # Constant time compare token against configured VERIFY_TOKEN
    if hmac.compare_digest(token, verify_token):
        return challenge

    return None


def parse_signed_request(
    signed_request: str,
    app_secret: str,
) -> dict[str, Any] | None:
    """Parse and verify a Meta signed_request parameter (e.g. data deletion callback).

    Meta signed_request format: '<encoded_sig>.<encoded_payload>'
    - encoded_sig is HMAC-SHA256 signature of encoded_payload, base64url encoded.
    - encoded_payload is JSON payload, base64url encoded.

    Returns:
        Parsed JSON dictionary if signature is valid, None otherwise.
    """
    if not signed_request or "." not in signed_request or not app_secret:
        return None

    try:
        encoded_sig, encoded_payload = signed_request.split(".", 1)
    except ValueError:
        return None

    # Base64url padding helper
    def _b64decode(s: str) -> bytes:
        padded = s + "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(padded)

    try:
        decoded_sig = _b64decode(encoded_sig)
        payload_bytes = _b64decode(encoded_payload)
        payload_dict = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    # Validate algorithm
    if (
        not isinstance(payload_dict, dict)
        or payload_dict.get("algorithm") != "HMAC-SHA256"
    ):
        return None

    # Verify signature
    expected_sig = hmac.new(
        key=app_secret.encode("utf-8"),
        msg=encoded_payload.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(expected_sig, decoded_sig):
        return None

    return payload_dict
