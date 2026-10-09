"""Internal administration routes for monitoring, kill switch control, and queue management."""

import hmac
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy import desc, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from meta_bot.api.deps import get_db_session_dep, get_redis_dep
from meta_bot.config import Settings, get_settings
from meta_bot.domain.enums import ActionStatus
from meta_bot.infra.db.models import ActionModel, AuditLogModel
from meta_bot.infra.redis.keys import (
    circuit_breaker_errors_key,
    circuit_breaker_state_key,
    daily_counter_key,
    dead_letter_queue_key,
    event_queue_key,
    hourly_counter_key,
    kill_switch_key,
    send_queue_key,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def verify_admin_token(
    authorization: Annotated[str | None, Header()] = None,
    x_admin_token: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = ...,  # type: ignore[assignment]
) -> None:
    """Validate admin authorization header against configured ADMIN_TOKEN."""
    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_admin_token:
        token = x_admin_token.strip()

    expected = settings.ADMIN_TOKEN.get_secret_value()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


class KillSwitchRequest(BaseModel):
    """Payload to toggle the global kill switch."""

    active: bool
    reason: str = "manual_admin_toggle"
    actor: str = "admin"
    model_config = ConfigDict(extra="ignore")


class CircuitBreakerResetRequest(BaseModel):
    """Payload to manually reset the circuit breaker."""

    actor: str = "admin"
    model_config = ConfigDict(extra="ignore")


class SystemStatusResponse(BaseModel):
    """Health, metrics, and pacing counter status for administrative monitoring."""

    kill_switch_active: bool
    circuit_breaker_state: str
    circuit_breaker_errors: int
    queue_depth_sends: int
    queue_depth_events: int
    queue_depth_dlq: int
    hourly_cap: int
    hourly_count: int
    daily_cap: int
    daily_count: int
    demo_mode: bool
    now: datetime


class ActionSummary(BaseModel):
    """Representation of an outbound action in admin views."""

    id: UUID
    platform: str
    kind: str
    status: str
    scheduled_at: datetime
    sent_at: datetime | None
    attempts: int
    error_code: str | None
    external_id: str | None


@router.post("/kill-switch", dependencies=[Depends(verify_admin_token)])
async def set_kill_switch(
    req: KillSwitchRequest,
    redis: Annotated[Redis, Depends(get_redis_dep)],
    session: Annotated[AsyncSession, Depends(get_db_session_dep)],
) -> dict[str, Any]:
    """Toggle the global kill switch on or off with audit recording."""
    await redis.set(kill_switch_key(), "1" if req.active else "0")

    # Record in audit log
    audit_stmt = insert(AuditLogModel).values(
        actor=req.actor,
        action="KILL_SWITCH_TOGGLED",
        details={"active": req.active, "reason": req.reason},
    )
    await session.execute(audit_stmt)
    await session.commit()

    logger.warning(
        "Kill switch state updated by admin", active=req.active, actor=req.actor
    )
    return {
        "status": "success",
        "kill_switch_active": req.active,
        "message": f"Kill switch is now {'ENABLED (all sends blocked)' if req.active else 'DISABLED'}",
    }


@router.post("/circuit-breaker/reset", dependencies=[Depends(verify_admin_token)])
async def reset_circuit_breaker(
    req: CircuitBreakerResetRequest,
    redis: Annotated[Redis, Depends(get_redis_dep)],
    session: Annotated[AsyncSession, Depends(get_db_session_dep)],
) -> dict[str, Any]:
    """Manually reset the circuit breaker to closed state."""
    await redis.delete(circuit_breaker_errors_key())
    await redis.set(circuit_breaker_state_key(), "closed")

    audit_stmt = insert(AuditLogModel).values(
        actor=req.actor,
        action="CIRCUIT_BREAKER_RESET",
        details={"state": "closed"},
    )
    await session.execute(audit_stmt)
    await session.commit()

    logger.info("Circuit breaker reset by admin", actor=req.actor)
    return {"status": "success", "circuit_breaker_state": "closed"}


@router.get(
    "/status",
    response_model=SystemStatusResponse,
    dependencies=[Depends(verify_admin_token)],
)
async def get_system_status(
    redis: Annotated[Redis, Depends(get_redis_dep)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SystemStatusResponse:
    """Return operational health, queues depth, and pacing counters."""
    now = datetime.now(timezone.utc)

    # Kill switch
    ks_val = await redis.get(kill_switch_key())
    kill_switch_active = settings.KILL_SWITCH_DEFAULT
    if ks_val is not None:
        ks_str = ks_val.decode("utf-8") if isinstance(ks_val, bytes) else str(ks_val)
        kill_switch_active = ks_str.lower() in ("1", "true", "yes", "on")

    # Circuit breaker
    cb_val = await redis.get(circuit_breaker_state_key())
    cb_state = "closed"
    if cb_val is not None:
        cb_state = (
            cb_val.decode("utf-8") if isinstance(cb_val, bytes) else str(cb_val)
        ).lower()

    cb_err_val = await redis.get(circuit_breaker_errors_key())
    cb_errors = int(cb_err_val) if cb_err_val else 0

    # Queue depths
    q_sends = int(await redis.zcard(send_queue_key()))
    q_events = int(await redis.llen(event_queue_key()))
    q_dlq = int(await redis.llen(dead_letter_queue_key()))

    # Counters
    hour_bucket = now.strftime("%Y%m%d%H")
    date_bucket = now.strftime("%Y%m%d")
    h_val = await redis.get(hourly_counter_key(settings.PAGE_ID, hour_bucket))
    hourly_count = int(h_val) if h_val else 0

    d_val = await redis.get(daily_counter_key(settings.PAGE_ID, date_bucket))
    daily_count = int(d_val) if d_val else 0

    return SystemStatusResponse(
        kill_switch_active=kill_switch_active,
        circuit_breaker_state=cb_state,
        circuit_breaker_errors=cb_errors,
        queue_depth_sends=q_sends,
        queue_depth_events=q_events,
        queue_depth_dlq=q_dlq,
        hourly_cap=settings.MAX_PRIVATE_PER_HOUR,
        hourly_count=hourly_count,
        daily_cap=settings.MAX_PRIVATE_PER_DAY,
        daily_count=daily_count,
        demo_mode=settings.DEMO_MODE,
        now=now,
    )


@router.get(
    "/actions",
    response_model=list[ActionSummary],
    dependencies=[Depends(verify_admin_token)],
)
async def list_actions(
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: Annotated[AsyncSession, Depends(get_db_session_dep)] = ...,  # type: ignore[assignment]
) -> list[ActionSummary]:
    """List recent outbound actions with optional status filtering."""
    stmt = select(ActionModel).order_by(desc(ActionModel.created_at))
    if status_filter:
        stmt = stmt.where(ActionModel.status == status_filter.upper())
    stmt = stmt.limit(limit).offset(offset)

    res = await session.execute(stmt)
    actions = res.scalars().all()

    return [
        ActionSummary(
            id=a.id,
            platform=a.platform,
            kind=a.kind,
            status=a.status,
            scheduled_at=a.scheduled_at,
            sent_at=a.sent_at,
            attempts=a.attempts,
            error_code=a.error_code,
            external_id=a.external_id,
        )
        for a in actions
    ]


@router.post("/actions/{action_id}/retry", dependencies=[Depends(verify_admin_token)])
async def retry_action(
    action_id: UUID,
    redis: Annotated[Redis, Depends(get_redis_dep)],
    session: Annotated[AsyncSession, Depends(get_db_session_dep)],
) -> dict[str, Any]:
    """Re-schedule a failed or skipped action for immediate re-execution."""
    res = await session.execute(select(ActionModel).where(ActionModel.id == action_id))
    action = res.scalar_one_or_none()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    now = datetime.now(timezone.utc)
    action.status = ActionStatus.QUEUED.value
    action.scheduled_at = now
    action.error_code = None
    await session.commit()

    # Re-insert into Redis sorted set with current timestamp
    await redis.zadd(send_queue_key(), {str(action.id): now.timestamp()})

    return {"status": "success", "message": f"Action {action_id} requeued"}


@router.post("/actions/{action_id}/cancel", dependencies=[Depends(verify_admin_token)])
async def cancel_action(
    action_id: UUID,
    redis: Annotated[Redis, Depends(get_redis_dep)],
    session: Annotated[AsyncSession, Depends(get_db_session_dep)],
) -> dict[str, Any]:
    """Cancel a queued action before it is dispatched."""
    res = await session.execute(select(ActionModel).where(ActionModel.id == action_id))
    action = res.scalar_one_or_none()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    # Remove from Redis sorted set
    await redis.zrem(send_queue_key(), str(action.id))

    action.status = ActionStatus.SKIPPED.value
    action.error_code = "CANCELLED_BY_ADMIN"
    await session.commit()

    return {"status": "success", "message": f"Action {action_id} cancelled"}
