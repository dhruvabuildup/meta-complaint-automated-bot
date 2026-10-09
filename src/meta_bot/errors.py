"""Exception hierarchy and Meta Graph API error classification."""

import re
from typing import Any


class BotError(Exception):
    """Base exception class for all meta_bot domain and operational errors."""


class EncryptionError(BotError):
    """Raised when token encryption or decryption fails."""


class MetaApiError(BotError):
    """Base exception for Meta Graph API call failures.

    Never logs or surfaces raw access tokens in error messages.
    """

    def __init__(
        self,
        status_code: int,
        meta_code: int | None = None,
        meta_subcode: int | None = None,
        message: str = "",
        fbtrace_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.meta_code = meta_code
        self.meta_subcode = meta_subcode
        self.fbtrace_id = fbtrace_id
        # Sanitize any potential token patterns in message text
        sanitized_message = _sanitize_error_message(message)
        self.message = sanitized_message
        super().__init__(
            f"Meta API error (status={status_code}, code={meta_code}, "
            f"subcode={meta_subcode}, fbtrace_id={fbtrace_id}): {sanitized_message}"
        )


class MetaAuthError(MetaApiError):
    """Raised when access token is invalid, expired, or session was invalidated."""


class MetaPermissionError(MetaApiError):
    """Raised when the Page/App lacks required scopes or assets permissions."""


class MetaRateLimitError(MetaApiError):
    """Raised when Graph API rate limits or pacing thresholds are exceeded."""

    def __init__(
        self,
        status_code: int,
        meta_code: int | None = None,
        meta_subcode: int | None = None,
        message: str = "",
        fbtrace_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
        )
        self.retry_after = retry_after


class MetaWindowError(MetaApiError):
    """Raised when outside the 24h messaging window or private reply is expired/already used.

    Meta rule: exactly one private reply per commenter within 7 days, and 24h messaging window.
    """


class MetaTransientError(MetaApiError):
    """Raised on 5xx server errors, timeouts, or temporary network/API failures."""


class MetaPermanentError(MetaApiError):
    """Raised on client errors that should not be automatically retried."""


def _sanitize_error_message(message: str) -> str:
    """Strip potential access tokens and secret patterns from error message text."""
    # Redact common token patterns (e.g. EAA... tokens, bearer headers)
    sanitized = re.sub(r"EAA[0-9A-Za-z]+", "[REDACTED_TOKEN]", message)
    sanitized = re.sub(
        r"(?i)bearer\s+[a-zA-Z0-9_\-\.]+", "Bearer [REDACTED]", sanitized
    )
    return sanitized


# Mapping of known Meta Graph API error codes/subcodes to exception types.
# TODO(verify): Confirm specific subcodes in Meta Graph API error documentation and App Dashboard Test console.
META_CODE_MAPPING: dict[int, type[MetaApiError]] = {
    # Authentication errors
    190: MetaAuthError,  # Invalid OAuth access token
    102: MetaAuthError,  # API Session error
    # Permission errors
    10: MetaPermissionError,  # Permission denied
    200: MetaPermissionError,  # Permissions error
    283: MetaPermissionError,  # Requires pages_messaging permission
    # Rate limit errors
    4: MetaRateLimitError,  # Application-level rate limit
    17: MetaRateLimitError,  # User-level rate limit
    32: MetaRateLimitError,  # Page-level rate limit
    613: MetaRateLimitError,  # Calls limit reached
    # Transient server errors
    1: MetaTransientError,  # An unknown error occurred / temporary
    2: MetaTransientError,  # Service temporarily unavailable
}

# Subcodes specific to Messenger / Instagram DM windows and private replies.
# TODO(verify): Validate subcodes 2018001, 2018047, 2018048 in Instagram Graph API testing
META_SUBCODE_MAPPING: dict[int, type[MetaApiError]] = {
    2018001: MetaWindowError,  # Private reply already sent for this comment
    2018047: MetaWindowError,  # Cannot send private reply: older than 7 days
    2018048: MetaWindowError,  # Cannot message user outside 24-hour messaging window
    2018065: MetaWindowError,  # Message recipient cannot be messaged
}


def map_meta_error(
    response_json: dict[str, Any] | None,
    status_code: int,
    retry_after: float | None = None,
) -> MetaApiError:
    """Map Meta API response and HTTP status into an appropriate MetaApiError subclass.

    Args:
        response_json: Parsed JSON payload returned by Meta, if available.
        status_code: HTTP status code from response.
        retry_after: Optional Retry-After delay in seconds.

    Returns:
        Instance of specific MetaApiError subclass.
    """
    error_data: dict[str, Any] = {}
    if response_json and isinstance(response_json.get("error"), dict):
        error_data = response_json["error"]

    meta_code: int | None = error_data.get("code")
    meta_subcode: int | None = error_data.get("error_subcode")
    message: str = str(error_data.get("message", ""))
    fbtrace_id: str | None = error_data.get("fbtrace_id")
    is_transient: bool = bool(error_data.get("is_transient", False))

    # 1. Check specific subcodes first (e.g. messaging window violations)
    if meta_subcode is not None and meta_subcode in META_SUBCODE_MAPPING:
        subcode_cls = META_SUBCODE_MAPPING[meta_subcode]
        return subcode_cls(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
        )

    # 2. Check specific error codes
    if meta_code is not None and meta_code in META_CODE_MAPPING:
        code_cls = META_CODE_MAPPING[meta_code]
        if code_cls is MetaRateLimitError:
            return MetaRateLimitError(
                status_code=status_code,
                meta_code=meta_code,
                meta_subcode=meta_subcode,
                message=message,
                fbtrace_id=fbtrace_id,
                retry_after=retry_after,
            )
        return code_cls(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
        )

    # 3. Transient flag from Meta payload
    if is_transient:
        return MetaTransientError(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
        )

    # 4. HTTP status fallbacks
    if status_code == 429:
        return MetaRateLimitError(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
            retry_after=retry_after,
        )
    if status_code == 401:
        return MetaAuthError(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
        )
    if status_code == 403:
        return MetaPermissionError(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
        )
    if status_code >= 500:
        return MetaTransientError(
            status_code=status_code,
            meta_code=meta_code,
            meta_subcode=meta_subcode,
            message=message,
            fbtrace_id=fbtrace_id,
        )

    return MetaPermanentError(
        status_code=status_code,
        meta_code=meta_code,
        meta_subcode=meta_subcode,
        message=message,
        fbtrace_id=fbtrace_id,
    )
