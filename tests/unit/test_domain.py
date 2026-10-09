"""Unit tests for domain models, rules, and clock protocols."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from meta_bot.domain.enums import ConversationState, Platform
from meta_bot.domain.models import (
    CommentEvent,
    SystemClock,
)
from meta_bot.domain.rules import (
    can_initiate_bot_reply,
    is_within_dm_window,
    is_within_private_reply_window,
)


def test_models_are_frozen() -> None:
    """Domain models must be immutable (frozen)."""
    now = datetime.now(timezone.utc)
    comment = CommentEvent(
        platform=Platform.INSTAGRAM,
        comment_id="c_123",
        post_id="p_456",
        commenter_id="u_789",
        text="Hello world",
        created_at=now,
    )
    with pytest.raises(ValidationError):
        # Mutating a frozen model raises ValidationError
        comment.text = "New text"


def test_system_clock() -> None:
    """Test SystemClock returns timezone-aware current UTC time."""
    clock = SystemClock()
    t1 = clock.now()
    assert t1.tzinfo is not None


@pytest.mark.parametrize(
    ("delta_hours", "expected"),
    [
        (0, True),
        (1, True),
        (23, True),
        (24, True),
        (25, False),
        (48, False),
    ],
)
def test_dm_window_rule(delta_hours: int, expected: bool) -> None:
    """Meta rule: 24h customer messaging window."""
    now = datetime.now(timezone.utc)
    last_msg_at = now - timedelta(hours=delta_hours)
    assert is_within_dm_window(last_msg_at, now, window_hours=24) is expected


@pytest.mark.parametrize(
    ("delta_days", "expected"),
    [
        (0, True),
        (1, True),
        (6, True),
        (7, True),
        (8, False),
        (30, False),
    ],
)
def test_private_reply_window_rule(delta_days: int, expected: bool) -> None:
    """Meta rule: exactly one private reply within 7 days."""
    now = datetime.now(timezone.utc)
    comment_at = now - timedelta(days=delta_days)
    assert is_within_private_reply_window(comment_at, now, window_days=7) is expected


@pytest.mark.parametrize(
    ("state", "opted_out", "in_window", "expected"),
    [
        (ConversationState.NEW, False, True, True),
        (ConversationState.BOT, False, True, True),
        (ConversationState.OPTED_OUT, False, True, False),
        (ConversationState.NEW, True, True, False),
        (ConversationState.HUMAN, False, True, True),
        (ConversationState.QUIET, False, True, False),
        (ConversationState.CLOSED, False, True, False),
        (ConversationState.BOT, False, False, False),
    ],
)
def test_can_initiate_bot_reply_rules(
    state: ConversationState,
    opted_out: bool,
    in_window: bool,
    expected: bool,
) -> None:
    """Test bot reply gating rules."""
    assert can_initiate_bot_reply(state, opted_out, in_window) is expected
