"""SQLAlchemy implementations of service repository protocols."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from meta_bot.domain.enums import ActionStatus, EventKind, Platform, SendKind
from meta_bot.infra.db.models import ActionModel, EventRawModel
from meta_bot.services.ports import ActionRepository, RawEventRepository


class SqlAlchemyActionRepository(ActionRepository):
    """Repository handling action execution records and private reply locking."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_private_reply(
        self,
        platform: Platform,
        contact_id: UUID,
        comment_id: UUID,
        body_hash: str,
        scheduled_at: datetime | None = None,
    ) -> bool:
        """Attempt to atomically claim a private reply for a comment.

        Meta rule: EXACTLY ONE private reply per comment.
        Uses INSERT ... ON CONFLICT DO NOTHING to guarantee idempotency.

        Returns:
            True if newly claimed, False if already claimed or conflict occurred.
        """
        now = scheduled_at or datetime.now(timezone.utc)
        stmt = (
            insert(ActionModel)
            .values(
                kind=SendKind.PRIVATE_REPLY.value,
                platform=platform.value,
                contact_id=contact_id,
                comment_id=comment_id,
                body_hash=body_hash,
                status=ActionStatus.QUEUED.value,
                scheduled_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["comment_id"],
                index_where=text("kind = 'PRIVATE_REPLY'"),
            )
        )
        result = await self._session.execute(stmt)
        if isinstance(result, CursorResult):
            return result.rowcount > 0
        return False


class SqlAlchemyRawEventRepository(RawEventRepository):
    """Repository handling audit storage and deduplication for raw webhook events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_event(
        self,
        platform: Platform,
        event_kind: EventKind,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> bool:
        """Insert incoming webhook payload with deduplication.

        Returns:
            True if payload was newly recorded, False if already received.
        """
        stmt = (
            insert(EventRawModel)
            .values(
                platform=platform.value,
                event_kind=event_kind.value,
                dedupe_key=dedupe_key,
                payload=payload,
                status="RECEIVED",
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
        result = await self._session.execute(stmt)
        if isinstance(result, CursorResult):
            return result.rowcount > 0
        return False
