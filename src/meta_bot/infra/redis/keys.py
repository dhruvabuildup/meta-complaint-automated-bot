"""Redis key naming schemes with uniform prefixes and versioning."""

REDIS_KEY_PREFIX = "mb:v1"


def dedupe_key(event_id: str) -> str:
    """Generate key for webhook event deduplication cache."""
    return f"{REDIS_KEY_PREFIX}:dedupe:{event_id}"


def kill_switch_key() -> str:
    """Generate key for the global automation kill switch flag."""
    return f"{REDIS_KEY_PREFIX}:system:kill_switch"


def hourly_counter_key(account_id: str, hour_bucket: str) -> str:
    """Generate key for hourly private reply rate limit tracking.

    Args:
        account_id: Facebook Page or Instagram account ID.
        hour_bucket: Formatted string representing hour bucket (e.g. YYYYMMDDHH).
    """
    return f"{REDIS_KEY_PREFIX}:rate:hourly:{account_id}:{hour_bucket}"


def daily_counter_key(account_id: str, date_bucket: str) -> str:
    """Generate key for daily private reply rate limit tracking.

    Args:
        account_id: Facebook Page or Instagram account ID.
        date_bucket: Formatted string representing date bucket (e.g. YYYYMMDD).
    """
    return f"{REDIS_KEY_PREFIX}:rate:daily:{account_id}:{date_bucket}"


def send_queue_key() -> str:
    """Generate key for sorted set send queue scheduled by Unix timestamp."""
    return f"{REDIS_KEY_PREFIX}:queue:sends"


def contact_lock_key(platform: str, contact_id: str) -> str:
    """Generate key for per-contact distributed concurrency lock."""
    return f"{REDIS_KEY_PREFIX}:lock:contact:{platform.lower()}:{contact_id}"


def event_queue_key() -> str:
    """Generate key for inbound webhook ingestion queue."""
    return f"{REDIS_KEY_PREFIX}:queue:events"


def event_processing_queue_key() -> str:
    """Generate key for in-flight event processing list."""
    return f"{REDIS_KEY_PREFIX}:queue:events:processing"


def dead_letter_queue_key() -> str:
    """Generate key for poison / failed events dead-letter list."""
    return f"{REDIS_KEY_PREFIX}:queue:events:dlq"


def circuit_breaker_errors_key() -> str:
    """Generate key for circuit breaker consecutive error counter."""
    return f"{REDIS_KEY_PREFIX}:circuit_breaker:consecutive_errors"


def circuit_breaker_state_key() -> str:
    """Generate key for circuit breaker state (open/closed)."""
    return f"{REDIS_KEY_PREFIX}:circuit_breaker:state"


def contact_body_key(contact_id: str, body_hash: str) -> str:
    """Generate key for tracking identical message hash sent to a contact."""
    return f"{REDIS_KEY_PREFIX}:guard:contact_body:{contact_id}:{body_hash}"


def post_body_key(post_id: str, body_hash: str) -> str:
    """Generate key for tracking identical public reply hash count on a post."""
    return f"{REDIS_KEY_PREFIX}:guard:post_body:{post_id}:{body_hash}"


def post_last_variant_key(post_id: str) -> str:
    """Generate key for the last used public reply variant ID on a post."""
    return f"{REDIS_KEY_PREFIX}:guard:post_last_variant:{post_id}"
