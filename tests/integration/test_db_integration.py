"""Integration tests for database schema and unique constraints."""

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from meta_bot.domain.enums import ActionStatus, Platform, SendKind
from meta_bot.infra.db.models import ActionModel, Base, CommentModel, ContactModel
from meta_bot.infra.db.repositories import SqlAlchemyActionRepository
from meta_bot.infra.db.session import create_session_factory


def get_test_db_url() -> str:
    """Retrieve test database connection URL."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgrespassword@localhost:5432/meta_bot_test",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_schema_create_and_drop() -> None:
    """Test schema table creation and teardown cycle."""
    db_url = get_test_db_url()
    engine = create_async_engine(db_url)

    # Check database connectivity
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"Database unavailable for integration test: {exc}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_constraint_rejects_second_private_reply() -> None:
    """Meta rule: EXACTLY ONE private reply per comment.

    The database unique partial index must reject a second private reply for the same comment.
    """
    db_url = get_test_db_url()
    engine = create_async_engine(db_url)

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"Database unavailable for integration test: {exc}")

    # Ensure schema tables exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    test_comment_id = uuid4()
    test_contact_id = uuid4()
    now_utc = datetime.now(timezone.utc)

    async with session_factory() as session:
        # Create prerequisite contact and comment records
        contact = ContactModel(
            id=test_contact_id,
            platform=Platform.INSTAGRAM.value,
            external_id=f"ext_{uuid4().hex[:8]}",
        )
        comment = CommentModel(
            id=test_comment_id,
            platform=Platform.INSTAGRAM.value,
            comment_id=f"comment_{uuid4().hex[:8]}",
            post_id="post_1",
            commenter_external_id="ext_user_1",
            text="Hello test comment",
            created_at=now_utc,
        )
        session.add(contact)
        session.add(comment)
        await session.commit()

        repo = SqlAlchemyActionRepository(session)

        # 1. First claim succeeds
        claimed_first = await repo.claim_private_reply(
            platform=Platform.INSTAGRAM,
            contact_id=test_contact_id,
            comment_id=test_comment_id,
            body_hash="hash_1",
        )
        await session.commit()
        assert claimed_first is True

        # 2. Second claim for same comment_id returns False (on conflict do nothing)
        claimed_second = await repo.claim_private_reply(
            platform=Platform.INSTAGRAM,
            contact_id=test_contact_id,
            comment_id=test_comment_id,
            body_hash="hash_2",
        )
        assert claimed_second is False

        # 3. Direct insert without ON CONFLICT raises IntegrityError
        duplicate_action = ActionModel(
            kind=SendKind.PRIVATE_REPLY.value,
            platform=Platform.INSTAGRAM.value,
            contact_id=test_contact_id,
            comment_id=test_comment_id,
            body_hash="hash_3",
            status=ActionStatus.QUEUED.value,
            scheduled_at=now_utc,
        )
        session.add(duplicate_action)
        with pytest.raises(IntegrityError):
            await session.commit()

    await engine.dispose()
