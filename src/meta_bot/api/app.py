"""FastAPI application factory, lifespan management, middleware, and health routes."""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request

from meta_bot.adapters.meta.graph_client import GraphClient
from meta_bot.api.deps import get_db_session_dep, get_redis_dep
from meta_bot.config import Settings, get_settings
from meta_bot.infra.db.engine import create_engine
from meta_bot.infra.db.session import create_session_factory
from meta_bot.infra.redis.client import get_redis_client
from meta_bot.logging_setup import set_correlation_id, setup_logging


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Middleware extracting or generating correlation ID for request tracing."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        corr_id = (
            request.headers.get("X-Correlation-ID")
            or request.headers.get("X-Request-ID")
            or uuid.uuid4().hex
        )
        set_correlation_id(corr_id)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = corr_id
            response.headers["X-Request-ID"] = corr_id
            return response
        finally:
            set_correlation_id(None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application resource lifecycle across startup and shutdown."""
    settings: Settings = getattr(app.state, "settings", None) or get_settings()
    setup_logging(settings.LOG_LEVEL)

    # Initialize infrastructure
    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)
    redis_client = get_redis_client(settings.REDIS_URL)
    graph_client = GraphClient(settings)

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis = redis_client
    app.state.graph_client = graph_client

    try:
        yield
    finally:
        # Graceful cleanup
        await graph_client.close()
        await redis_client.aclose()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI application factory."""
    resolved_settings = settings or get_settings()

    app = FastAPI(
        title="Meta Comment & DM Automation Bot",
        version="0.1.0",
        docs_url="/docs" if resolved_settings.APP_ENV != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings

    # Add middlewares
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/healthz", tags=["Health"], summary="Liveness check")
    async def healthz() -> dict[str, str]:
        """Simple liveness probe indicating process is responsive."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["Health"], summary="Readiness check")
    async def readyz(
        response: Response,
        db: Annotated[AsyncSession, Depends(get_db_session_dep)],
        redis: Annotated[Redis, Depends(get_redis_dep)],
    ) -> dict[str, str]:
        """Readiness probe verifying PostgreSQL and Redis connectivity."""
        checks: dict[str, str] = {
            "status": "ready",
            "database": "unknown",
            "redis": "unknown",
        }

        # Check Postgres
        try:
            await db.execute(text("SELECT 1"))
            checks["database"] = "healthy"
        except (SQLAlchemyError, OSError) as exc:
            checks["database"] = f"unhealthy: {exc}"
            checks["status"] = "not_ready"

        # Check Redis
        try:
            await redis.ping()
            checks["redis"] = "healthy"
        except (RedisError, OSError) as exc:
            checks["redis"] = f"unhealthy: {exc}"
            checks["status"] = "not_ready"

        if checks["status"] != "ready":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return checks

    return app
