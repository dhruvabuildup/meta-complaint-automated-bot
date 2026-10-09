"""FastAPI dependency providers for settings, database sessions, and clients."""

from collections.abc import AsyncGenerator
from typing import cast

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meta_bot.config import Settings
from meta_bot.services.ports import GraphApi


def get_settings_dep(request: Request) -> Settings:
    """Provide current application settings."""
    return cast(Settings, request.app.state.settings)


def get_redis_dep(request: Request) -> Redis:
    """Provide application Redis client."""
    return cast(Redis, request.app.state.redis)


def get_graph_client_dep(request: Request) -> GraphApi:
    """Provide application Meta Graph API client."""
    return cast(GraphApi, request.app.state.graph_client)


async def get_db_session_dep(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Provide an asynchronous database session per request."""
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
