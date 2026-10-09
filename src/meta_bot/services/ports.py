"""Service ports (Protocols) decoupling application and domain from infrastructure adapters."""

from typing import Any, Protocol
from uuid import UUID

from meta_bot.domain.enums import EventKind, Platform


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


class ActionRepository(Protocol):
    """Protocol for persisting and claiming outbound actions."""

    async def claim_private_reply(
        self,
        platform: Platform,
        contact_id: UUID,
        comment_id: UUID,
        body_hash: str,
    ) -> bool:
        """Attempt to claim sending a private reply for a specific comment.

        Returns True if claimed successfully, False if already claimed (idempotency).
        Meta rule: exactly one private reply per comment.
        """
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
