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
    HANDED_OFF = "HANDED_OFF"
    QUIET = "QUIET"
    OPTED_OUT = "OPTED_OUT"
    CLOSED = "CLOSED"


class GuardReason(StrEnum):
    """Evaluation reason returned by SendGuard."""

    ALLOWED = "ALLOWED"
    KILL_SWITCH = "KILL_SWITCH"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    CONTACT_OPTED_OUT = "CONTACT_OPTED_OUT"
    HOURLY_CAP_REACHED = "HOURLY_CAP_REACHED"
    DAILY_CAP_REACHED = "DAILY_CAP_REACHED"
    DUPLICATE_BODY = "DUPLICATE_BODY"
    MAX_IDENTICAL_PUBLIC_REACHED = "MAX_IDENTICAL_PUBLIC_REACHED"
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"


class HandoffReason(StrEnum):
    """Reason for handing off conversation to a human agent."""

    COMPLAINT = "COMPLAINT"
    REFUND = "REFUND"
    ANGER = "ANGER"
    LEGAL = "LEGAL"
    PAYMENT = "PAYMENT"
    SAFETY = "SAFETY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    MANUAL = "MANUAL"


class IntentGroup(StrEnum):
    """Categorized intent group for inbound comment text matching."""

    PRICE = "PRICE"
    LINK = "LINK"
    LOCATION = "LOCATION"
    INFO = "INFO"
    COMPLAINT = "COMPLAINT"
    GENERAL = "GENERAL"


class ActionStatus(StrEnum):
    """Execution lifecycle status of an automated action."""

    QUEUED = "QUEUED"
    SENT = "SENT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class DecisionKind(StrEnum):
    """Pipeline filter outcome."""

    PROCEED = "PROCEED"
    DROP = "DROP"


class DropReason(StrEnum):
    """Reason why an incoming event was filtered out in the pipeline."""

    UNSUPPORTED_EVENT = "UNSUPPORTED_EVENT"
    EDIT_OR_DELETE = "EDIT_OR_DELETE"
    OWN_EVENT = "OWN_EVENT"
    SPAM_OR_PAGE = "SPAM_OR_PAGE"
    DUPLICATE = "DUPLICATE"
    THREADED_REPLY_IGNORED = "THREADED_REPLY_IGNORED"
    PRIVATE_REPLY_ALREADY_CLAIMED = "PRIVATE_REPLY_ALREADY_CLAIMED"
    CONTACT_OPTED_OUT = "CONTACT_OPTED_OUT"
