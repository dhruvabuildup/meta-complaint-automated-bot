"""Domain models, value objects, and clock protocol."""

from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from meta_bot.domain.enums import (
    ActionStatus,
    DecisionKind,
    DropReason,
    Platform,
    SendKind,
)


class CommentId(BaseModel):
    """Strongly typed comment identifier."""

    value: str
    model_config = ConfigDict(frozen=True)

    def __str__(self) -> str:
        return self.value


class PostId(BaseModel):
    """Strongly typed post identifier."""

    value: str
    model_config = ConfigDict(frozen=True)

    def __str__(self) -> str:
        return self.value


class ContactId(BaseModel):
    """Strongly typed customer/contact platform-specific identifier."""

    value: str
    model_config = ConfigDict(frozen=True)

    def __str__(self) -> str:
        return self.value


class ActionId(BaseModel):
    """Strongly typed internal action identifier."""

    value: str
    model_config = ConfigDict(frozen=True)

    def __str__(self) -> str:
        return self.value


class CommentEvent(BaseModel):
    """Incoming comment parsed from Meta webhook feed or comments subscription.

    Meta rule: Instagram sends comments via 'comments' field, Facebook via 'feed' (item=comment).
    """

    model_config = ConfigDict(frozen=True)

    platform: Platform
    comment_id: str
    parent_comment_id: str | None = None
    post_id: str
    ad_id: str | None = None
    commenter_id: str
    commenter_username: str | None = None
    text: str
    created_at: datetime
    raw_payload_id: str | None = None


class MessageEvent(BaseModel):
    """Incoming direct message or Messenger interaction.

    Meta rule: customer messages open or refresh the standard 24-hour service window.
    """

    model_config = ConfigDict(frozen=True)

    platform: Platform
    message_id: str
    sender_id: str
    recipient_id: str
    text: str
    created_at: datetime
    is_echo: bool = False
    raw_payload_id: str | None = None


class SendRequest(BaseModel):
    """Outbound send request to be processed by senders.

    Meta rules enforced:
    - PUBLIC_REPLY: posts comment reply
    - PRIVATE_REPLY: sends exactly one private DM from comment within 7 days
    - DM: sends inside active 24h conversation window
    """

    model_config = ConfigDict(frozen=True)

    kind: SendKind
    platform: Platform
    recipient_id: str
    comment_id: str | None = None
    text: str
    scheduled_at: datetime | None = None
    template_id: str | None = None


class SendResult(BaseModel):
    """Outcome of attempting to execute a SendRequest."""

    model_config = ConfigDict(frozen=True)

    action_id: str | None = None
    status: ActionStatus
    sent_at: datetime | None = None
    external_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class Clock(Protocol):
    """Time source protocol allowing injectable time in tests."""

    def now(self) -> datetime:
        """Return current datetime with timezone."""
        ...


class SystemClock:
    """Production clock returning current UTC time."""

    def now(self) -> datetime:
        """Return current UTC datetime."""
        return datetime.now(timezone.utc)


class PipelineDecision(BaseModel):
    """Result of passing an incoming webhook event through the filter pipeline."""

    model_config = ConfigDict(frozen=True)

    kind: DecisionKind
    event: CommentEvent | MessageEvent | None = None
    drop_reason: DropReason | None = None
    details: dict[str, str] | None = None
