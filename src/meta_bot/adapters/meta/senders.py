"""Meta Graph API outbound message sender adapter implementing MessageSender protocol."""

from datetime import datetime, timezone
from typing import Any

import structlog

from meta_bot.config import Settings, get_settings
from meta_bot.domain.enums import ActionStatus, Platform
from meta_bot.domain.models import SendResult
from meta_bot.services.ports import GraphApi, MessageSender

logger = structlog.get_logger(__name__)


class MetaMessageSender(MessageSender):
    """Adapter for sending public replies, private replies, and DMs via Meta Graph API."""

    def __init__(
        self,
        graph_client: GraphApi,
        settings: Settings | None = None,
    ) -> None:
        self._graph = graph_client
        self._settings = settings or get_settings()

    async def public_reply(
        self,
        platform: Platform,
        comment_id: str,
        text: str,
    ) -> SendResult:
        """Post a public comment reply.

        Instagram endpoint: POST /{ig-comment-id}/replies?message=...
        Facebook endpoint: POST /{comment-id}/comments?message=...
        """
        now = datetime.now(timezone.utc)
        if self._settings.DEMO_MODE:
            logger.info(
                "DEMO_MODE active: simulating public reply send",
                platform=platform.value,
                comment_id=comment_id,
            )
            return SendResult(
                status=ActionStatus.SENT,
                sent_at=now,
                external_id=f"demo_pub_{comment_id}",
                message_id=f"demo_pub_{comment_id}",
                raw_status="demo_sent",
            )

        path = (
            f"{comment_id}/replies"
            if platform == Platform.INSTAGRAM
            else f"{comment_id}/comments"
        )
        payload = {"message": text}
        res = await self._graph.post(path=path, json=payload)

        # Meta returns {"id": "..."} for created replies
        msg_id = str(res.get("id", ""))
        return SendResult(
            status=ActionStatus.SENT,
            sent_at=now,
            external_id=msg_id,
            message_id=msg_id,
            raw_status="sent",
        )

    async def private_reply(
        self,
        page_id: str,
        comment_id: str,
        text: str,
    ) -> SendResult:
        """Send a single private reply DM from a comment.

        Meta rule: exactly one private reply per commenter within 7 days.
        Endpoint: POST /{PAGE_ID}/messages
        Payload: {"recipient": {"comment_id": comment_id}, "message": {"text": text}}
        # TODO(verify): Confirm exact recipient comment_id payload structure in current Meta Graph API docs.
        """
        now = datetime.now(timezone.utc)
        if self._settings.DEMO_MODE:
            logger.info(
                "DEMO_MODE active: simulating private reply send",
                page_id=page_id,
                comment_id=comment_id,
            )
            return SendResult(
                status=ActionStatus.SENT,
                sent_at=now,
                external_id=f"demo_priv_{comment_id}",
                message_id=f"demo_priv_{comment_id}",
                raw_status="demo_sent",
            )

        path = f"{page_id}/messages"
        # TODO(verify): Validate recipient payload structure across Instagram Professional and Facebook Pages
        payload = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": text},
        }
        res = await self._graph.post(path=path, json=payload)

        # Meta returns {"message_id": "...", "recipient_id": "..."}
        msg_id = str(res.get("message_id") or res.get("id") or "")
        return SendResult(
            status=ActionStatus.SENT,
            sent_at=now,
            external_id=msg_id,
            message_id=msg_id,
            raw_status="sent",
        )

    async def send_dm(
        self,
        recipient_id: str,
        text: str,
        buttons: list[dict[str, Any]] | None = None,
        quick_replies: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        """Send a standard two-way DM inside the 24-hour customer service window."""
        now = datetime.now(timezone.utc)
        if self._settings.DEMO_MODE:
            logger.info(
                "DEMO_MODE active: simulating direct message send",
                recipient_id=recipient_id,
            )
            return SendResult(
                status=ActionStatus.SENT,
                sent_at=now,
                external_id=f"demo_dm_{recipient_id}",
                message_id=f"demo_dm_{recipient_id}",
                raw_status="demo_sent",
            )

        page_id = self._settings.PAGE_ID
        path = f"{page_id}/messages"
        msg_obj: dict[str, Any] = {"text": text}
        if quick_replies:
            msg_obj["quick_replies"] = quick_replies
        if buttons:
            msg_obj["attachment"] = {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": text,
                    "buttons": buttons,
                },
            }

        payload: dict[str, Any] = {
            "recipient": {"id": recipient_id},
            "message": msg_obj,
        }
        res = await self._graph.post(path=path, json=payload)
        msg_id = str(res.get("message_id") or res.get("id") or "")
        return SendResult(
            status=ActionStatus.SENT,
            sent_at=now,
            external_id=msg_id,
            message_id=msg_id,
            raw_status="sent",
        )
