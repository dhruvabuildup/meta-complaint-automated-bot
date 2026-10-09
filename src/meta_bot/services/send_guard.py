"""Outbound message guard enforcing Meta compliance, rate caps, pacing, and circuit breaking."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import structlog
from redis.asyncio import Redis

from meta_bot.config import Settings, get_settings
from meta_bot.domain.enums import GuardReason, SendKind
from meta_bot.domain.models import Clock, GuardDecision, SendContext, SystemClock
from meta_bot.domain.rules import (
    is_within_dm_window,
    is_within_private_reply_window,
)
from meta_bot.errors import MetaAuthError, MetaPermissionError
from meta_bot.infra.redis.keys import (
    circuit_breaker_errors_key,
    circuit_breaker_state_key,
    contact_body_key,
    daily_counter_key,
    hourly_counter_key,
    kill_switch_key,
    post_body_key,
)

logger = structlog.get_logger(__name__)


def compute_body_hash(text: str) -> str:
    """Compute sha256 hash of message body for deduplication checks."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


class SendGuard:
    """The single outbound choke point enforcing all Meta policies and system safeguards.

    Every outbound message must pass SendGuard.check(context) before calling senders.
    """

    def __init__(
        self,
        redis_client: Redis,
        settings: Settings | None = None,
        clock: Clock | None = None,
        alert_hook: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.redis = redis_client
        self.settings = settings or get_settings()
        self.clock = clock or SystemClock()
        self.alert_hook = alert_hook

    async def is_kill_switch_active(self) -> bool:
        """Check if global kill switch is currently enabled in Redis."""
        val = await self.redis.get(kill_switch_key())
        if val is None:
            return self.settings.KILL_SWITCH_DEFAULT
        val_str = val.decode("utf-8") if isinstance(val, bytes) else str(val)
        return val_str.lower() in ("1", "true", "yes", "on")

    async def set_kill_switch(self, active: bool) -> None:
        """Explicitly set global kill switch state in Redis."""
        await self.redis.set(kill_switch_key(), "1" if active else "0")

    async def is_circuit_breaker_open(self) -> bool:
        """Check if circuit breaker has tripped due to consecutive Meta errors."""
        val = await self.redis.get(circuit_breaker_state_key())
        if val is None:
            return False
        val_str = val.decode("utf-8") if isinstance(val, bytes) else str(val)
        return val_str.lower() == "open"

    async def check(self, context: SendContext) -> GuardDecision:
        """Evaluate an outbound message request against all compliance and pacing rules.

        Evaluation Order:
        1. Global Kill Switch
        2. Circuit Breaker State
        3. Contact Opt-out
        4. Never Initiate / Timing Windows (7-day private reply, 24h DM)
        5. Duplicate Message Guard
        6. Hourly & Daily Rate Caps
        """
        now = self.clock.now()

        # Rule 1: Kill switch off (Redis flag)
        if await self.is_kill_switch_active():
            return GuardDecision(
                allowed=False,
                reason=GuardReason.KILL_SWITCH,
                details={"reason": "kill_switch_active"},
            )

        # Rule 2: Circuit breaker state
        if await self.is_circuit_breaker_open():
            return GuardDecision(
                allowed=False,
                reason=GuardReason.CIRCUIT_BREAKER_OPEN,
                details={"reason": "circuit_breaker_tripped"},
            )

        # Rule 3: Contact opt-out blocks all sends immediately
        if context.is_opted_out:
            return GuardDecision(
                allowed=False,
                reason=GuardReason.CONTACT_OPTED_OUT,
                details={"contact_id": context.contact_id},
            )

        # Rule 4: Never initiate cold DMs (enforce timing windows)
        if context.send_kind == SendKind.PRIVATE_REPLY:
            if context.comment_created_at is None:
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.OUTSIDE_WINDOW,
                    details={"reason": "missing_comment_created_at"},
                )
            if not is_within_private_reply_window(
                comment_created_at=context.comment_created_at,
                current_time=now,
                window_days=self.settings.PRIVATE_REPLY_WINDOW_DAYS,
            ):
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.OUTSIDE_WINDOW,
                    details={"reason": "comment_older_than_7_days"},
                )

        elif context.send_kind == SendKind.DM:
            if context.last_user_message_at is None:
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.OUTSIDE_WINDOW,
                    details={"reason": "missing_last_user_message_at"},
                )
            if not is_within_dm_window(
                last_user_message_at=context.last_user_message_at,
                current_time=now,
                window_hours=self.settings.DM_WINDOW_HOURS,
            ):
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.OUTSIDE_WINDOW,
                    details={"reason": "outside_24h_service_window"},
                )

        # Rule 5: Duplicate message guard
        body_hash = compute_body_hash(context.body)
        if context.send_kind in (SendKind.PRIVATE_REPLY, SendKind.DM):
            # Same body_hash to the same contact never sent twice
            c_key = contact_body_key(context.contact_id, body_hash)
            if await self.redis.exists(c_key):
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.DUPLICATE_BODY,
                    details={"contact_id": context.contact_id, "body_hash": body_hash},
                )
        elif context.send_kind == SendKind.PUBLIC_REPLY and context.post_id:
            # Public replies to the same post cap identical body count
            p_key = post_body_key(context.post_id, body_hash)
            existing_count_raw = await self.redis.get(p_key)
            existing_count = int(existing_count_raw) if existing_count_raw else 0
            if existing_count >= self.settings.MAX_IDENTICAL_PUBLIC_PER_POST:
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.MAX_IDENTICAL_PUBLIC_REACHED,
                    details={
                        "post_id": context.post_id,
                        "body_hash": body_hash,
                        "count": str(existing_count),
                    },
                )

        # Rule 6: Hourly and daily caps (only private replies and DMs)
        if context.send_kind in (SendKind.PRIVATE_REPLY, SendKind.DM):
            account_id = context.account_id
            hour_bucket = now.strftime("%Y%m%d%H")
            date_bucket = now.strftime("%Y%m%d")

            h_key = hourly_counter_key(account_id, hour_bucket)
            d_key = daily_counter_key(account_id, date_bucket)

            h_val = await self.redis.get(h_key)
            hourly_count = int(h_val) if h_val else 0
            if hourly_count >= self.settings.MAX_PRIVATE_PER_HOUR:
                # Re-schedule for start of next hour
                next_hour = (now + timedelta(hours=1)).replace(
                    minute=0, second=5, microsecond=0
                )
                retry_after = max(int((next_hour - now).total_seconds()), 60)
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.HOURLY_CAP_REACHED,
                    retry_after_seconds=retry_after,
                    details={"cap": str(self.settings.MAX_PRIVATE_PER_HOUR)},
                )

            d_val = await self.redis.get(d_key)
            daily_count = int(d_val) if d_val else 0
            if daily_count >= self.settings.MAX_PRIVATE_PER_DAY:
                # Re-schedule for start of next day
                next_day = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=10, microsecond=0
                )
                retry_after = max(int((next_day - now).total_seconds()), 300)
                return GuardDecision(
                    allowed=False,
                    reason=GuardReason.DAILY_CAP_REACHED,
                    retry_after_seconds=retry_after,
                    details={"cap": str(self.settings.MAX_PRIVATE_PER_DAY)},
                )

        return GuardDecision(allowed=True, reason=GuardReason.ALLOWED)

    async def record_send_success(self, context: SendContext) -> None:
        """Update Redis rate counters and duplicate guards following a successful send."""
        now = self.clock.now()
        body_hash = compute_body_hash(context.body)

        if context.send_kind in (SendKind.PRIVATE_REPLY, SendKind.DM):
            account_id = context.account_id
            hour_bucket = now.strftime("%Y%m%d%H")
            date_bucket = now.strftime("%Y%m%d")

            h_key = hourly_counter_key(account_id, hour_bucket)
            d_key = daily_counter_key(account_id, date_bucket)

            pipe = self.redis.pipeline()
            pipe.incr(h_key)
            pipe.expire(h_key, 7200)  # 2 hours TTL
            pipe.incr(d_key)
            pipe.expire(d_key, 172800)  # 2 days TTL

            # Record contact duplicate body hash (30 days TTL)
            c_key = contact_body_key(context.contact_id, body_hash)
            pipe.set(c_key, "1", ex=2592000)
            await pipe.execute()

        elif context.send_kind == SendKind.PUBLIC_REPLY and context.post_id:
            p_key = post_body_key(context.post_id, body_hash)
            pipe = self.redis.pipeline()
            pipe.incr(p_key)
            pipe.expire(p_key, 604800)  # 7 days TTL
            await pipe.execute()

    async def record_meta_error(self, exc: Exception) -> bool:
        """Process an error returned by Meta and trip circuit breaker if threshold is hit.

        Returns True if circuit breaker was tripped, False otherwise.
        """
        is_critical = isinstance(exc, (MetaAuthError, MetaPermissionError))
        # Also check for policy enforcement signals
        exc_str = str(exc).lower()
        if "enforcement" in exc_str or "policy" in exc_str:
            is_critical = True

        if not is_critical:
            return False

        counter_key = circuit_breaker_errors_key()
        count = await self.redis.incr(counter_key)
        logger.warning(
            "Consecutive critical Meta error recorded",
            count=count,
            threshold=self.settings.CIRCUIT_BREAKER_ERROR_THRESHOLD,
            error_type=exc.__class__.__name__,
        )

        if count >= self.settings.CIRCUIT_BREAKER_ERROR_THRESHOLD:
            # Trip the breaker and enable kill switch immediately
            await self.redis.set(circuit_breaker_state_key(), "open")
            await self.set_kill_switch(True)

            logger.critical(
                "CIRCUIT BREAKER TRIPPED: Meta critical error threshold exceeded; kill switch activated",
                consecutive_errors=count,
                error_type=exc.__class__.__name__,
            )

            if self.alert_hook:
                try:
                    await self.alert_hook(
                        "CIRCUIT_BREAKER_TRIPPED",
                        {
                            "consecutive_errors": count,
                            "error": str(exc),
                            "error_type": exc.__class__.__name__,
                        },
                    )
                except Exception as hook_exc:  # noqa: BLE001
                    logger.error("Failed to invoke alert hook", error=str(hook_exc))

            return True

        return False

    async def record_send_attempt_success(self) -> None:
        """Reset consecutive error counter on any successful API interaction."""
        await self.redis.delete(circuit_breaker_errors_key())

    async def reset_circuit_breaker(self, actor: str) -> None:
        """Manually reset circuit breaker (half-open / recovery by admin action only)."""
        logger.info("Admin reset circuit breaker", actor=actor)
        await self.redis.delete(circuit_breaker_errors_key())
        await self.redis.set(circuit_breaker_state_key(), "closed")
