"""SQLAlchemy implementations of service repository protocols."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from meta_bot.domain.enums import ActionStatus, EventKind, Platform, SendKind
from meta_bot.domain.models import CommentEvent
from meta_bot.infra.db.models import (
    ActionModel,
    CommentModel,
    ContactModel,
    EventRawModel,
)
from meta_bot.services.ports import (
    ActionRepository,
    CommentRepositoryPort,
    ContactRepositoryPort,
    RawEventRepository,
)


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
        """Create a new action record with payload and schedule time."""
        stmt = (
            insert(ActionModel)
            .values(
                platform=platform.value,
                kind=kind.value,
                contact_id=contact_id,
                comment_id=comment_id,
                conversation_id=conversation_id,
                template_id=template_id,
                body_hash=body_hash,
                payload_text=payload_text,
                status=ActionStatus.QUEUED.value,
                scheduled_at=scheduled_at,
            )
            .returning(ActionModel.id)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one()

    async def get_action(self, action_id: UUID) -> ActionModel | None:
        """Retrieve action by ID."""
        stmt = select(ActionModel).where(ActionModel.id == action_id)
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none()

    async def update_action_status(
        self,
        action_id: UUID,
        status: ActionStatus,
        sent_at: datetime | None = None,
        external_id: str | None = None,
        error_code: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        """Update action lifecycle status and execution metrics."""
        values: dict[str, Any] = {
            "status": status.value,
            "updated_at": func.now(),
        }
        if sent_at is not None:
            values["sent_at"] = sent_at
        if external_id is not None:
            values["external_id"] = external_id
        if error_code is not None:
            values["error_code"] = error_code
        if increment_attempts:
            values["attempts"] = ActionModel.attempts + 1

        stmt = update(ActionModel).where(ActionModel.id == action_id).values(**values)
        await self._session.execute(stmt)


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

    async def mark_processed(
        self,
        dedupe_key: str,
        status: str,
    ) -> None:
        """Mark a raw event as processed with timestamp and outcome status."""
        stmt = (
            update(EventRawModel)
            .where(EventRawModel.dedupe_key == dedupe_key)
            .values(
                status=status,
                processed_at=func.now(),
            )
        )
        await self._session.execute(stmt)


class SqlAlchemyContactRepository(ContactRepositoryPort):
    """Repository managing contact records and opt-out tracking."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_contact(
        self,
        platform: Platform,
        external_id: str,
        username: str | None = None,
    ) -> tuple[UUID, bool]:
        """Find or create contact record atomically.

        Returns:
            Tuple of (contact_id, is_opted_out).
        """
        stmt = (
            insert(ContactModel)
            .values(
                platform=platform.value,
                external_id=external_id,
                username=username,
            )
            .on_conflict_do_update(
                index_elements=["platform", "external_id"],
                set_={
                    "username": func.coalesce(username, ContactModel.username),
                    "updated_at": func.now(),
                },
            )
            .returning(ContactModel.id, ContactModel.opted_out)
        )
        res = await self._session.execute(stmt)
        row = res.one()
        return (row[0], bool(row[1]))

    async def set_opt_out(
        self,
        platform: Platform,
        external_id: str,
        opted_out: bool = True,
    ) -> None:
        """Update contact opt-out status."""
        stmt = (
            update(ContactModel)
            .where(
                ContactModel.platform == platform.value,
                ContactModel.external_id == external_id,
            )
            .values(opted_out=opted_out, updated_at=func.now())
        )
        await self._session.execute(stmt)


class SqlAlchemyCommentRepository(CommentRepositoryPort):
    """Repository managing comment persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_comment(self, comment: CommentEvent) -> UUID:
        """Insert or update comment record atomically.

        Returns:
            Internal UUID of the persisted comment.
        """
        stmt = (
            insert(CommentModel)
            .values(
                platform=comment.platform.value,
                comment_id=comment.comment_id,
                parent_comment_id=comment.parent_comment_id,
                post_id=comment.post_id,
                ad_id=comment.ad_id,
                commenter_external_id=comment.commenter_id,
                commenter_username=comment.commenter_username,
                text=comment.text,
                created_at=comment.created_at,
            )
            .on_conflict_do_update(
                index_elements=["comment_id"],
                set_={"text": comment.text},
            )
            .returning(CommentModel.id)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one()
