"""Unit tests for SendGuard rules, rate limits, duplicate guards, and circuit breaking."""

from datetime import datetime, timedelta, timezone
from typing import Any

import fakeredis.aioredis
import pytest

from meta_bot.config import Settings
from meta_bot.domain.enums import GuardReason, Platform, SendKind
from meta_bot.domain.models import SendContext
from meta_bot.errors import MetaAuthError, MetaPermissionError, MetaTransientError
from meta_bot.services.send_guard import SendGuard


class MockClock:
    """Deterministic injectable clock for testing time-sensitive rules."""

    def __init__(self, current_time: datetime) -> None:
        self._current_time = current_time

    def now(self) -> datetime:
        return self._current_time

    def advance(self, duration: timedelta) -> None:
        self._current_time += duration


@pytest.fixture
def mock_clock() -> MockClock:
    """Fixture providing fixed UTC datetime."""
    return MockClock(datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_rule1_kill_switch_blocks_sends(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
) -> None:
    """Rule 1: If global kill switch is on, all sends are refused immediately."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.INSTAGRAM,
        contact_id="user_123",
        send_kind=SendKind.PUBLIC_REPLY,
        body="Hello world",
    )

    # 1. Kill switch off -> allowed
    decision = await guard.check(context)
    assert decision.allowed is True
    assert decision.reason == GuardReason.ALLOWED

    # 2. Kill switch activated -> blocked
    await guard.set_kill_switch(True)
    decision = await guard.check(context)
    assert decision.allowed is False
    assert decision.reason == GuardReason.KILL_SWITCH


@pytest.mark.asyncio
async def test_rule2_circuit_breaker_open_blocks_sends(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
) -> None:
    """Rule 2: If circuit breaker state is open, sends are blocked."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.INSTAGRAM,
        contact_id="user_123",
        send_kind=SendKind.PUBLIC_REPLY,
        body="Hello world",
    )

    # Simulate circuit breaker open
    await fake_redis.set("mb:v1:circuit_breaker:state", "open")
    decision = await guard.check(context)
    assert decision.allowed is False
    assert decision.reason == GuardReason.CIRCUIT_BREAKER_OPEN


@pytest.mark.asyncio
async def test_rule3_contact_opt_out_blocks_all_sends(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
) -> None:
    """Rule 3: STOP / opt-out blocks any outbound send to that contact."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    # Opted-out contact
    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.INSTAGRAM,
        contact_id="user_opted_out",
        is_opted_out=True,
        send_kind=SendKind.PRIVATE_REPLY,
        comment_created_at=mock_clock.now() - timedelta(minutes=5),
        body="Here is info",
    )

    decision = await guard.check(context)
    assert decision.allowed is False
    assert decision.reason == GuardReason.CONTACT_OPTED_OUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "comment_age,expected_allowed,expected_reason",
    [
        (timedelta(minutes=5), True, GuardReason.ALLOWED),
        (timedelta(days=6, hours=23), True, GuardReason.ALLOWED),
        (timedelta(days=7, minutes=1), False, GuardReason.OUTSIDE_WINDOW),
        (timedelta(days=10), False, GuardReason.OUTSIDE_WINDOW),
    ],
)
async def test_rule4_private_reply_7_day_window(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
    comment_age: timedelta,
    expected_allowed: bool,
    expected_reason: GuardReason,
) -> None:
    """Rule 4a: Private reply is permitted ONLY within 7 days of the comment creation."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.INSTAGRAM,
        contact_id="user_123",
        send_kind=SendKind.PRIVATE_REPLY,
        comment_created_at=mock_clock.now() - comment_age,
        body="Hello from bot",
    )

    decision = await guard.check(context)
    assert decision.allowed is expected_allowed
    assert decision.reason == expected_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_age,expected_allowed,expected_reason",
    [
        (timedelta(minutes=10), True, GuardReason.ALLOWED),
        (timedelta(hours=23, minutes=50), True, GuardReason.ALLOWED),
        (timedelta(hours=24, minutes=5), False, GuardReason.OUTSIDE_WINDOW),
        (timedelta(days=2), False, GuardReason.OUTSIDE_WINDOW),
    ],
)
async def test_rule4_dm_24_hour_window(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
    message_age: timedelta,
    expected_allowed: bool,
    expected_reason: GuardReason,
) -> None:
    """Rule 4b: Standard two-way DM is permitted ONLY within 24 hours of user's last message."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.FACEBOOK,
        contact_id="user_fb_123",
        send_kind=SendKind.DM,
        last_user_message_at=mock_clock.now() - message_age,
        body="Reply inside 24h window",
    )

    decision = await guard.check(context)
    assert decision.allowed is expected_allowed
    assert decision.reason == expected_reason


@pytest.mark.asyncio
async def test_rule5_duplicate_message_guard_contact(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
) -> None:
    """Rule 5a: Same body text never sent twice to the same contact."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.INSTAGRAM,
        contact_id="user_unique_contact",
        send_kind=SendKind.PRIVATE_REPLY,
        comment_created_at=mock_clock.now() - timedelta(minutes=5),
        body="Identical greeting text here",
    )

    # First send is allowed
    decision1 = await guard.check(context)
    assert decision1.allowed is True

    # Record send success
    await guard.record_send_success(context)

    # Second send with identical body is blocked
    decision2 = await guard.check(context)
    assert decision2.allowed is False
    assert decision2.reason == GuardReason.DUPLICATE_BODY


@pytest.mark.asyncio
async def test_rule5_duplicate_public_reply_cap_per_post(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
) -> None:
    """Rule 5b: Identical public reply text capped at MAX_IDENTICAL_PUBLIC_PER_POST."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.INSTAGRAM,
        contact_id="user_456",
        post_id="post_999",
        send_kind=SendKind.PUBLIC_REPLY,
        body="Check your DMs!",
    )

    # Send up to cap (settings default is 3)
    for _ in range(test_settings.MAX_IDENTICAL_PUBLIC_PER_POST):
        dec = await guard.check(context)
        assert dec.allowed is True
        await guard.record_send_success(context)

    # Next attempt with identical text on this post is blocked
    dec_capped = await guard.check(context)
    assert dec_capped.allowed is False
    assert dec_capped.reason == GuardReason.MAX_IDENTICAL_PUBLIC_REACHED


@pytest.mark.asyncio
async def test_rule6_hourly_and_daily_rate_caps(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
) -> None:
    """Rule 6: Hourly and daily caps trigger re-scheduling with retry-after."""
    guard = SendGuard(redis_client=fake_redis, settings=test_settings, clock=mock_clock)

    context = SendContext(
        account_id=test_settings.PAGE_ID,
        platform=Platform.INSTAGRAM,
        contact_id="user_rate_test",
        send_kind=SendKind.PRIVATE_REPLY,
        comment_created_at=mock_clock.now() - timedelta(minutes=5),
        body="Paced DM",
    )

    # Simulate reaching hourly cap in Redis
    hour_bucket = mock_clock.now().strftime("%Y%m%d%H")
    h_key = f"mb:v1:rate:hourly:{test_settings.PAGE_ID}:{hour_bucket}"
    await fake_redis.set(h_key, str(test_settings.MAX_PRIVATE_PER_HOUR))

    dec_hourly = await guard.check(context)
    assert dec_hourly.allowed is False
    assert dec_hourly.reason == GuardReason.HOURLY_CAP_REACHED
    assert dec_hourly.retry_after_seconds is not None
    assert dec_hourly.retry_after_seconds > 0

    # Reset hourly, simulate daily cap
    await fake_redis.delete(h_key)
    date_bucket = mock_clock.now().strftime("%Y%m%d")
    d_key = f"mb:v1:rate:daily:{test_settings.PAGE_ID}:{date_bucket}"
    await fake_redis.set(d_key, str(test_settings.MAX_PRIVATE_PER_DAY))

    dec_daily = await guard.check(context)
    assert dec_daily.allowed is False
    assert dec_daily.reason == GuardReason.DAILY_CAP_REACHED
    assert dec_daily.retry_after_seconds is not None
    assert dec_daily.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_consecutive_critical_errors(
    fake_redis: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_clock: MockClock,
) -> None:
    """Rule 6: Consecutive critical Meta errors trip circuit breaker and engage kill switch."""
    alert_events: list[dict[str, Any]] = []

    async def mock_alert(event_type: str, details: dict[str, Any]) -> None:
        alert_events.append({"event_type": event_type, "details": details})

    guard = SendGuard(
        redis_client=fake_redis,
        settings=test_settings,
        clock=mock_clock,
        alert_hook=mock_alert,
    )

    # Transient error does NOT trip breaker
    transient_exc = MetaTransientError(status_code=503, message="Gateway timeout")
    tripped = await guard.record_meta_error(transient_exc)
    assert tripped is False
    assert await guard.is_circuit_breaker_open() is False

    # Simulate consecutive MetaAuthErrors up to threshold
    auth_exc = MetaAuthError(status_code=401, message="Session invalidated")
    threshold = test_settings.CIRCUIT_BREAKER_ERROR_THRESHOLD  # 5

    for _ in range(threshold - 1):
        tripped = await guard.record_meta_error(auth_exc)
        assert tripped is False
        assert await guard.is_circuit_breaker_open() is False

    # 5th consecutive error trips the breaker!
    perm_exc = MetaPermissionError(status_code=403, message="App permissions revoked")
    tripped = await guard.record_meta_error(perm_exc)
    assert tripped is True
    assert await guard.is_circuit_breaker_open() is True
    assert await guard.is_kill_switch_active() is True
    assert len(alert_events) == 1
    assert alert_events[0]["event_type"] == "CIRCUIT_BREAKER_TRIPPED"

    # Admin reset restores circuit breaker
    await guard.reset_circuit_breaker(actor="admin_user")
    assert await guard.is_circuit_breaker_open() is False
