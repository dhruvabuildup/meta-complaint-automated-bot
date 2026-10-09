"""Webhook routes for Meta verification handshake and event ingestion."""

import hashlib
import json
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from meta_bot.api.deps import get_db_session_dep, get_redis_dep, get_settings_dep
from meta_bot.api.security import verify_signature, verify_webhook_challenge
from meta_bot.config import Settings
from meta_bot.domain.enums import EventKind, Platform
from meta_bot.infra.db.repositories import SqlAlchemyRawEventRepository
from meta_bot.infra.redis.keys import event_queue_key

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhook"])


@router.get("", summary="Meta Webhook Verification Handshake")
async def verify_webhook(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    """Handle GET verification handshake request initiated by Meta App Dashboard.

    Meta rule: GET verification requires matching hub.mode='subscribe' and configured
    verify token, and returning hub.challenge as plain text with 200.
    """
    challenge = verify_webhook_challenge(
        mode=hub_mode,
        token=hub_verify_token,
        challenge=hub_challenge,
        verify_token=settings.VERIFY_TOKEN.get_secret_value(),
    )
    if challenge is not None:
        return Response(
            content=challenge, media_type="text/plain", status_code=status.HTTP_200_OK
        )

    logger.warning("Webhook handshake failed", mode=hub_mode)
    return Response(
        content="Forbidden",
        media_type="text/plain",
        status_code=status.HTTP_403_FORBIDDEN,
    )


@router.post("", summary="Meta Webhook Event Ingestion")
async def receive_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    session: Annotated[AsyncSession, Depends(get_db_session_dep)],
    redis: Annotated[Redis, Depends(get_redis_dep)],
) -> Response:
    """Receive, authenticate, store, and enqueue incoming webhook events.

    Meta rule: Read raw body first, verify HMAC-SHA256 signature BEFORE parsing,
    return 200 immediately (under 200ms target), and process asynchronously.
    """
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    # 1. Verify signature BEFORE parsing
    is_valid = verify_signature(
        raw_body=raw_body,
        header=signature_header,
        app_secret=settings.APP_SECRET.get_secret_value(),
    )
    if not is_valid:
        logger.warning("Rejected webhook POST with invalid signature")
        return Response(
            content="Forbidden: Invalid signature",
            media_type="text/plain",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    # 2. Parse JSON payload
    try:
        payload_dict = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Malformed but signed payload: record for review and return 200 to prevent Meta retry loop
        body_digest = hashlib.sha256(raw_body).hexdigest()[:16]
        logger.error(
            "Received malformed JSON in signed webhook payload", digest=body_digest
        )
        raw_repo = SqlAlchemyRawEventRepository(session)
        await raw_repo.record_event(
            platform=Platform.FACEBOOK,
            event_kind=EventKind.ENFORCEMENT,
            dedupe_key=f"malformed:{body_digest}",
            payload={"raw_base64": raw_body.hex(), "status": "MALFORMED_REVIEW"},
        )
        return Response(
            content=json.dumps({"status": "malformed_recorded"}),
            media_type="application/json",
            status_code=status.HTTP_200_OK,
        )

    obj_type = str(payload_dict.get("object", "unknown")).lower()
    platform = Platform.INSTAGRAM if obj_type == "instagram" else Platform.FACEBOOK
    raw_repo = SqlAlchemyRawEventRepository(session)
    queue_key = event_queue_key()

    entries = payload_dict.get("entry", [])
    for entry in entries:
        entry_id = str(entry.get("id", ""))
        entry_time = entry.get("time")

        # 3. Changes (Feed / Comments)
        for change in entry.get("changes", []):
            field = str(change.get("field", "change"))
            val = change.get("value", {})
            event_id = (
                val.get("id") or val.get("comment_id") or f"evt_{uuid.uuid4().hex[:8]}"
            )
            dedupe_key = f"{obj_type}:{field}:{event_id}"

            await raw_repo.record_event(
                platform=platform,
                event_kind=EventKind.COMMENT,
                dedupe_key=dedupe_key,
                payload=change,
            )

            queue_item = {
                "object": obj_type,
                "entry_id": entry_id,
                "time": entry_time,
                "field": field,
                "value": val,
                "dedupe_key": dedupe_key,
            }
            await redis.rpush(queue_key, json.dumps(queue_item))
            logger.info(
                "Enqueued webhook change",
                object=obj_type,
                field=field,
                id=str(event_id)[:12],
            )

        # 4. Messaging (DMs / Messenger)
        for msg_item in entry.get("messaging", []):
            msg = msg_item.get("message", {})
            postback = msg_item.get("postback", {})
            mid = msg.get("mid") or postback.get("mid") or f"mid_{uuid.uuid4().hex[:8]}"
            dedupe_key = f"{obj_type}:messaging:{mid}"

            await raw_repo.record_event(
                platform=platform,
                event_kind=EventKind.MESSAGE,
                dedupe_key=dedupe_key,
                payload=msg_item,
            )

            queue_item = {
                "object": obj_type,
                "entry_id": entry_id,
                "time": entry_time,
                "messaging": [msg_item],
                "dedupe_key": dedupe_key,
            }
            await redis.rpush(queue_key, json.dumps(queue_item))
            logger.info("Enqueued webhook message", object=obj_type, mid=str(mid)[:12])

    return Response(
        content=json.dumps({"status": "ok"}),
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )
