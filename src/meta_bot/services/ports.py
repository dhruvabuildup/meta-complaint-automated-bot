from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from meta_bot.domain.enums import ActionStatus, EventKind, Platform, SendKind
from meta_bot.domain.models import CommentEvent, SendResult


class GraphApi(Protocol):
    """Protocol for interacting with the Meta Graph API."""

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Perform a GET request to the Graph API."""
        ...

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Perform a POST request to the Graph API."""
        ...

    async def debug_token(self, input_token: str) -> dict[str, Any]:
        """Inspect and debug an access token using app secret credentials."""
        ...

    async def get_page_instagram_account(self, page_id: str) -> dict[str, Any]:
        """Query Facebook Page to discover linked Instagram business account."""
        ...


class MessageSender(Protocol):
    """Protocol for dispatching outbound messages and replies to Meta Graph API."""

    async def public_reply(
        self,
        platform: Platform,
        comment_id: str,
        text: str,
    ) -> SendResult:
        """Reply publicly to a comment on Instagram or Facebook.

        Meta rule: varied wording, capped duplicate phrases.
        """
        ...

    async def private_reply(
        self,
        page_id: str,
        comment_id: str,
        text: str,
    ) -> SendResult:
        """Send a single private reply DM to a commenter.

        Meta rule: exactly one private reply per commenter within 7 days.
        """
        ...

    async def send_dm(
        self,
        recipient_id: str,
        text: str,
        buttons: list[dict[str, Any]] | None = None,
        quick_replies: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        """Send a direct message inside the active 24-hour window."""
        ...


class ActionRepository(Protocol):
    """Protocol for persisting and claiming outbound actions."""

    async def claim_private_reply(
        self,
        platform: Platform,
        contact_id: UUID,
        comment_id: UUID,
        body_hash: str,
        scheduled_at: datetime | None = None,
    ) -> bool:
        """Attempt to claim sending a private reply for a specific comment.

        Returns True if claimed successfully, False if already claimed (idempotency).
        Meta rule: exactly one private reply per comment.
        """
        ...

    async def create_action(
        self,
        platform: Platform,
        kind: SendKind,
        contact_id: UUID | None,
        comment_id: UUID | None,
        body_hash: str,
        payload_text: str,
        scheduled_at: datetime,
        template_id: UUID | None = None,
        conversation_id: UUID | None = None,
    ) -> UUID:
        """Create a new queued action record and return its internal ID."""
        ...

    async def update_action_status(
        self,
        action_id: UUID,
        status: ActionStatus,
        sent_at: datetime | None = None,
        external_id: str | None = None,
        error_code: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        """Update action status, attempts and external tracking ID."""
        ...


class RawEventRepository(Protocol):
    """Protocol for storing and deduplicating raw webhook payloads."""

    async def record_event(
        self,
        platform: Platform,
        event_kind: EventKind,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> bool:
        """Record raw webhook event with deduplication.

        Returns True if newly inserted, False if already processed.
        """
        ...

    async def mark_processed(
        self,
        dedupe_key: str,
        status: str,
    ) -> None:
        """Mark a raw event as processed with timestamp and outcome status."""
        ...


class DedupePort(Protocol):
    """Protocol for atomic deduplication cache."""

    async def set_nx(self, key: str, value: str, ttl_seconds: int = 604800) -> bool:
        """Atomically set key if not exists with TTL.

        Returns True if key was set (first time seen), False if already existed.
        """
        ...


class ContactRepositoryPort(Protocol):
    """Protocol for contact identity profile operations."""

    async def get_or_create_contact(
        self,
        platform: Platform,
        external_id: str,
        username: str | None = None,
    ) -> tuple[UUID, bool]:
        """Find or create contact record.

        Returns (contact_id, opted_out).
        """
        ...

    async def set_opt_out(
        self,
        platform: Platform,
        external_id: str,
        opted_out: bool = True,
    ) -> None:
        """Update contact opt-out status."""
        ...


class CommentRepositoryPort(Protocol):
    """Protocol for persisting and retrieving comments."""

    async def upsert_comment(self, comment: CommentEvent) -> UUID:
        """Insert or update comment and return internal database UUID."""
        ...
