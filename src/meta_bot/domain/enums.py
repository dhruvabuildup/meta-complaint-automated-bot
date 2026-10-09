"""Domain enumerations for platforms, event kinds, states and action statuses."""

from enum import StrEnum


class Platform(StrEnum):
    """Supported Meta social platforms."""

    INSTAGRAM = "INSTAGRAM"
    FACEBOOK = "FACEBOOK"


class EventKind(StrEnum):
    """Kinds of incoming Meta webhook events."""

    COMMENT = "COMMENT"
    MESSAGE = "MESSAGE"
    POSTBACK = "POSTBACK"
    ECHO = "ECHO"
    ENFORCEMENT = "ENFORCEMENT"


class SendKind(StrEnum):
    """Kinds of outbound communications."""

    PUBLIC_REPLY = "PUBLIC_REPLY"
    PRIVATE_REPLY = "PRIVATE_REPLY"
    DM = "DM"


class ConversationState(StrEnum):
    """Lifecycle state of a two-way contact conversation."""

    NEW = "NEW"
    BOT = "BOT"
    HUMAN = "HUMAN"
    QUIET = "QUIET"
    OPTED_OUT = "OPTED_OUT"
    CLOSED = "CLOSED"


class ActionStatus(StrEnum):
    """Execution lifecycle status of an automated action."""

    QUEUED = "QUEUED"
    SENT = "SENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    DEAD = "DEAD"
