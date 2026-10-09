"""Pure domain rules enforcing Meta policies, timing windows, and pacing safeguards.

Contains no I/O or external dependencies.
"""

from datetime import datetime, timedelta

from meta_bot.domain.enums import ConversationState


def is_within_dm_window(
    last_user_message_at: datetime,
    current_time: datetime,
    window_hours: int = 24,
) -> bool:
    """Check if contact interaction is inside the standard Meta 24-hour customer service window.

    Meta rule: bots can only reply to a user within 24 hours of the user's last message.
    """
    if last_user_message_at > current_time:
        return False
    return (current_time - last_user_message_at) <= timedelta(hours=window_hours)


def is_within_private_reply_window(
    comment_created_at: datetime,
    current_time: datetime,
    window_days: int = 7,
) -> bool:
    """Check if comment is within the 7-day window allowed for private reply DMs.

    Meta rule: exactly one private reply can be sent within 7 days of the comment creation.
    """
    if comment_created_at > current_time:
        return False
    return (current_time - comment_created_at) <= timedelta(days=window_days)


def can_initiate_bot_reply(
    state: ConversationState,
    is_opted_out: bool,
    is_in_window: bool,
) -> bool:
    """Determine whether automated bot response is legally permitted to be dispatched.

    Meta rule: STOP / opt-out must be honoured immediately before any logic.
    Bot must not initiate cold DMs outside active policy windows.
    """
    if is_opted_out or state == ConversationState.OPTED_OUT:
        return False
    if state in (ConversationState.CLOSED, ConversationState.QUIET):
        return False
    return is_in_window
