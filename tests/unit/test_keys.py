"""Unit tests for Redis key builders."""

from meta_bot.infra.redis import keys


def test_key_builders_uniform_prefix() -> None:
    """Ensure all keys start with the versioned prefix."""
    prefix = "mb:v1:"
    assert keys.dedupe_key("evt_123").startswith(prefix)
    assert keys.kill_switch_key().startswith(prefix)
    assert keys.hourly_counter_key("acc_1", "2026100912").startswith(prefix)
    assert keys.daily_counter_key("acc_1", "20261009").startswith(prefix)
    assert keys.send_queue_key().startswith(prefix)
    assert keys.contact_lock_key("instagram", "usr_999").startswith(prefix)


def test_contact_lock_key_lowercase_platform() -> None:
    """Platform in lock keys should be normalized to lowercase."""
    assert (
        keys.contact_lock_key("INSTAGRAM", "123") == "mb:v1:lock:contact:instagram:123"
    )
