"""Meta webhook payload schema models and domain event normalizers."""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meta_bot.domain.enums import Platform
from meta_bot.domain.models import CommentEvent, MessageEvent


class MetaUser(BaseModel):
    """User representation across Meta Graph webhook events."""

    model_config = ConfigDict(extra="ignore")

    id: str
    username: str | None = None
    name: str | None = None


class InstagramMedia(BaseModel):
    """Media object linked to an Instagram comment."""

    model_config = ConfigDict(extra="ignore")

    id: str
    media_product_type: str | None = None


class InstagramCommentValue(BaseModel):
    """Value payload for Instagram 'comments' field change."""

    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    # TODO(verify): Confirm if 'from' field is always present or can be omitted if user is private
    from_: MetaUser | None = Field(default=None, alias="from")
    media: InstagramMedia | None = None
    parent_id: str | None = None


class FacebookFeedValue(BaseModel):
    """Value payload for Facebook Page 'feed' field change."""

    model_config = ConfigDict(extra="ignore")

    item: str  # e.g., 'comment', 'status', 'reaction', 'post'
    verb: str  # e.g., 'add', 'edited', 'remove', 'hide'
    comment_id: str | None = None
    parent_id: str | None = None
    post_id: str | None = None
    from_: MetaUser | None = Field(default=None, alias="from")
    message: str | None = None
    # TODO(verify): Check whether created_time is epoch integer or ISO 8601 string across ad comments
    created_time: int | None = None


class WebhookChange(BaseModel):
    """Field change envelope within a webhook entry."""

    model_config = ConfigDict(extra="ignore")

    field: str
    value: dict[str, Any]


class MessagingMessage(BaseModel):
    """Direct message payload within a messaging event."""

    model_config = ConfigDict(extra="ignore")

    mid: str
    text: str | None = None
    is_echo: bool = False
    attachments: list[dict[str, Any]] | None = None
    quick_reply: dict[str, Any] | None = None


class MessagingPostback(BaseModel):
    """Postback interaction payload from Messenger or Instagram buttons."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    payload: str | None = None
    # TODO(verify): Verify if mid is always present on postback events in Graph v25.0
    mid: str | None = None


class MessagingItem(BaseModel):
    """Messaging entry envelope for Instagram Direct or Facebook Messenger."""

    model_config = ConfigDict(extra="ignore")

    sender: MetaUser
    recipient: MetaUser
    timestamp: int | None = None
    message: MessagingMessage | None = None
    postback: MessagingPostback | None = None
    delivery: dict[str, Any] | None = None
    read: dict[str, Any] | None = None
    referral: dict[str, Any] | None = None
    policy_enforcement: dict[str, Any] | None = None


class WebhookEntry(BaseModel):
    """Top-level entry element in a Meta webhook notification."""

    model_config = ConfigDict(extra="ignore")

    id: str  # Facebook Page ID or Instagram Account ID
    time: int | None = None
    changes: list[WebhookChange] | None = None
    messaging: list[MessagingItem] | None = None


class WebhookPayload(BaseModel):
    """Root container sent by Meta in webhook POST requests."""

    model_config = ConfigDict(extra="ignore")

    object: str  # 'instagram' or 'page'
    entry: list[WebhookEntry] = Field(default_factory=list)


def _timestamp_to_datetime(ts: int | None) -> datetime:
    """Safely convert epoch seconds or milliseconds to timezone-aware UTC datetime."""
    if not ts:
        return datetime.now(timezone.utc)
    # Detect milliseconds vs seconds
    if ts > 10_000_000_000:
        ts = ts // 1000
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OverflowError):
        return datetime.now(timezone.utc)


def normalize_instagram_comment(
    change: WebhookChange,
    entry_time: int | None = None,
) -> CommentEvent | None:
    """Normalize an Instagram webhook change into a domain CommentEvent.

    Meta rule: Instagram comments arrive with field='comments'.
    live_comments are parsed and ignored for now.
    """
    if change.field != "comments":
        return None

    try:
        val = InstagramCommentValue.model_validate(change.value)
    except (ValidationError, ValueError, TypeError):
        return None

    commenter_id = val.from_.id if val.from_ else ""
    commenter_username = val.from_.username if val.from_ else None
    post_id = val.media.id if val.media else ""

    return CommentEvent(
        platform=Platform.INSTAGRAM,
        comment_id=val.id,
        parent_comment_id=val.parent_id,
        post_id=post_id,
        ad_id=None,
        commenter_id=commenter_id,
        commenter_username=commenter_username,
        text=val.text,
        created_at=_timestamp_to_datetime(entry_time),
    )


def normalize_facebook_comment(
    change: WebhookChange,
    entry_time: int | None = None,
) -> CommentEvent | None:
    """Normalize a Facebook Page webhook change into a domain CommentEvent.

    Meta rule: Facebook Page comments arrive under field='feed' with item='comment' and verb='add'.
    Non-comment feed changes (reactions, posts, shares) or edits/removals are ignored.
    """
    if change.field != "feed":
        return None

    try:
        val = FacebookFeedValue.model_validate(change.value)
    except (ValidationError, ValueError, TypeError):
        return None

    # Only process new comment additions
    if val.item != "comment" or val.verb != "add" or not val.comment_id:
        return None

    commenter_id = val.from_.id if val.from_ else ""
    commenter_name = val.from_.name if val.from_ else None
    post_id = val.post_id or ""
    text_content = val.message or ""
    created_at = _timestamp_to_datetime(val.created_time or entry_time)

    return CommentEvent(
        platform=Platform.FACEBOOK,
        comment_id=val.comment_id,
        parent_comment_id=val.parent_id,
        post_id=post_id,
        ad_id=None,
        commenter_id=commenter_id,
        commenter_username=commenter_name,
        text=text_content,
        created_at=created_at,
    )


def normalize_messaging_item(
    platform: Platform,
    item: MessagingItem,
) -> MessageEvent | None:
    """Normalize an incoming message or postback into a domain MessageEvent.

    Delivery and read receipts are acknowledged and ignored.
    """
    # 1. Standard user message or echo
    if item.message:
        return MessageEvent(
            platform=platform,
            message_id=item.message.mid,
            sender_id=item.sender.id,
            recipient_id=item.recipient.id,
            text=item.message.text or "",
            created_at=_timestamp_to_datetime(item.timestamp),
            is_echo=item.message.is_echo,
        )

    # 2. Postback interaction
    if item.postback:
        mid = item.postback.mid or f"postback_{item.timestamp or 0}_{item.sender.id}"
        text_content = item.postback.payload or item.postback.title or ""
        return MessageEvent(
            platform=platform,
            message_id=mid,
            sender_id=item.sender.id,
            recipient_id=item.recipient.id,
            text=text_content,
            created_at=_timestamp_to_datetime(item.timestamp),
            is_echo=False,
        )

    # Delivery and read receipts return None
    return None
