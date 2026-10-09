"""Database engine configuration and lifecycle management."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(
    database_url: str,
    echo: bool = False,
    pool_size: int = 10,
    max_overflow: int = 20,
) -> AsyncEngine:
    """Create and configure an asynchronous SQLAlchemy engine.

    Args:
        database_url: Connection string using async driver (e.g. postgresql+asyncpg).
        echo: If True, echo SQL queries to stdout (use for debugging only).
        pool_size: Base connection pool capacity.
        max_overflow: Overflow connections permitted beyond pool_size.

    Returns:
        Configured AsyncEngine instance.
    """
    return create_async_engine(
        database_url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
    )
