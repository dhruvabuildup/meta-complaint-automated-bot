"""Worker-side event filter pipeline enforcing Meta platform and messaging policies."""

import hashlib
from contextlib import suppress
from typing import Any

import structlog
from pydantic import ValidationError

from meta_bot.adapters.meta.payloads import (
    WebhookChange,
    WebhookEntry,
    normalize_facebook_comment,
    normalize_instagram_comment,
    normalize_messaging_item,
)
from meta_bot.config import Settings
from meta_bot.domain.enums import DecisionKind, DropReason, Platform
from meta_bot.domain.models import Clock, CommentEvent, MessageEvent, PipelineDecision
from meta_bot.services.ports import (
    ActionRepository,
    CommentRepositoryPort,
    ContactRepositoryPort,
    DedupePort,
)

logger = structlog.get_logger(__name__)

DEFAULT_SPAM_PATTERNS: tuple[str, ...] = (
    "whatsapp",
    "t.me/",
    "telegram",
    "crypto",
    "forex",
    "invest",
    "dm me to earn",
)


def _compute_body_hash(text: str) -> str:
    """Compute sha256 hash of text for action audit."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class EventPipeline:
    """Event processing pipeline running policy checks in strict sequence."""

    def __init__(
        self,
        dedupe_port: DedupePort,
        contact_repo: ContactRepositoryPort,
        comment_repo: CommentRepositoryPort,
        action_repo: ActionRepository,
        clock: Clock,
        settings: Settings,
        spam_patterns: tuple[str, ...] = DEFAULT_SPAM_PATTERNS,
    ) -> None:
        self.dedupe = dedupe_port
        self.contact_repo = contact_repo
        self.comment_repo = comment_repo
        self.action_repo = action_repo
        self.clock = clock
        self.settings = settings
        self.spam_patterns = spam_patterns

    async def process_event(self, raw_item: dict[str, Any]) -> PipelineDecision:
        """Execute the 8-step filtering and ingestion pipeline.

        Filter Order:
        1. Supported event kind and not an edit or delete
        2. Ignore own comments/replies and echoes
        3. Ignore comments from other Pages or spam patterns
        4. Atomic dedupe in Redis (7-day TTL)
        5. Top-level comments only (unless replies-to-replies allowed)
        6. Exactly one private reply per commenter/comment (atomic claim)
        7. Respect contact opt-out
        8. Persist and return PROCEED
        """
        # Step 1: Check for edits, deletes, and unparseable events
        verb = raw_item.get("verb")
        if not verb and isinstance(raw_item.get("value"), dict):
            verb = raw_item["value"].get("verb")
        if verb in ("edited", "remove", "hide", "deleted"):
            return PipelineDecision(
                kind=DecisionKind.DROP,
                drop_reason=DropReason.EDIT_OR_DELETE,
                details={"verb": str(verb)},
            )

        event = self._extract_event(raw_item)
        if event is None:
            return PipelineDecision(
                kind=DecisionKind.DROP,
                drop_reason=DropReason.UNSUPPORTED_EVENT,
                details={"raw_item_keys": ",".join(raw_item.keys())},
            )

        # Step 2: Ignore own comments and echoes
        if isinstance(event, MessageEvent) and event.is_echo:
            return PipelineDecision(
                kind=DecisionKind.DROP,
                drop_reason=DropReason.OWN_EVENT,
                details={"reason": "message_echo"},
            )

        author_id = (
            event.commenter_id if isinstance(event, CommentEvent) else event.sender_id
        )
        if author_id in (self.settings.PAGE_ID, self.settings.IG_ACCOUNT_ID):
            return PipelineDecision(
                kind=DecisionKind.DROP,
                drop_reason=DropReason.OWN_EVENT,
                details={"author_id": author_id},
            )

        # Step 3: Ignore comments from other pages or spam patterns
        if isinstance(event, CommentEvent):
            text_lower = event.text.lower()
            if any(p in text_lower for p in self.spam_patterns):
                return PipelineDecision(
                    kind=DecisionKind.DROP,
                    drop_reason=DropReason.SPAM_OR_PAGE,
                    details={"text": "matched_spam_pattern"},
                )

        # Step 4: Atomic dedupe in Redis: SET key NX EX 604800 (7 days)
        # Meta retries up to 36h and ad comments duplicate, so this is race-safe
        event_id = (
            event.comment_id if isinstance(event, CommentEvent) else event.message_id
        )
        dedupe_key = f"mb:v1:dedupe:{event.platform.lower()}:{event_id}"
        is_first_seen = await self.dedupe.set_nx(
            key=dedupe_key,
            value="1",
            ttl_seconds=604800,
        )
        if not is_first_seen:
            return PipelineDecision(
                kind=DecisionKind.DROP,
                drop_reason=DropReason.DUPLICATE,
                details={"event_id": event_id},
            )

        # Step 5: Top-level comment policy (reply-to-reply gating)
        if (
            isinstance(event, CommentEvent)
            and event.parent_comment_id
            and not self.settings.ALLOW_REPLIES_TO_REPLIES
        ):
            return PipelineDecision(
                kind=DecisionKind.DROP,
                drop_reason=DropReason.THREADED_REPLY_IGNORED,
                details={"parent_comment_id": event.parent_comment_id},
            )

        # Step 6: Atomic claim for private reply (comments only)
        # Upsert contact & comment to obtain internal UUIDs
        if isinstance(event, CommentEvent):
            contact_id, is_opted_out = await self.contact_repo.get_or_create_contact(
                platform=event.platform,
                external_id=event.commenter_id,
                username=event.commenter_username,
            )
            comment_uuid = await self.comment_repo.upsert_comment(event)

            body_hash = _compute_body_hash(event.text)
            claimed = await self.action_repo.claim_private_reply(
                platform=event.platform,
                contact_id=contact_id,
                comment_id=comment_uuid,
                body_hash=body_hash,
            )
            if not claimed:
                return PipelineDecision(
                    kind=DecisionKind.DROP,
                    drop_reason=DropReason.PRIVATE_REPLY_ALREADY_CLAIMED,
                    details={"comment_id": event.comment_id},
                )

            # Step 7: Respect contact opt-out
            if is_opted_out:
                return PipelineDecision(
                    kind=DecisionKind.DROP,
                    drop_reason=DropReason.CONTACT_OPTED_OUT,
                    details={"contact_id": str(contact_id)},
                )
        else:
            # For MessageEvent: check opt-out
            contact_id, is_opted_out = await self.contact_repo.get_or_create_contact(
                platform=event.platform,
                external_id=event.sender_id,
            )
            # Step 7: Respect contact opt-out
            if is_opted_out:
                return PipelineDecision(
                    kind=DecisionKind.DROP,
                    drop_reason=DropReason.CONTACT_OPTED_OUT,
                    details={"contact_id": str(contact_id)},
                )

        # Step 8: Persist and return PROCEED
        logger.info(
            "Event passed pipeline checks",
            event_type=event.__class__.__name__,
            platform=event.platform,
        )
        return PipelineDecision(
            kind=DecisionKind.PROCEED,
            event=event,
        )

    def _extract_event(
        self, raw_item: dict[str, Any]
    ) -> CommentEvent | MessageEvent | None:
        """Attempt to parse dictionary into normalized CommentEvent or MessageEvent."""
        # 1. Direct model pass-through
        if "comment_id" in raw_item and "platform" in raw_item:
            with suppress(ValidationError, ValueError, TypeError):
                return CommentEvent.model_validate(raw_item)
        if "message_id" in raw_item and "platform" in raw_item:
            with suppress(ValidationError, ValueError, TypeError):
                return MessageEvent.model_validate(raw_item)

        # 2. Instagram comment change
        if raw_item.get("field") == "comments":
            change = WebhookChange(
                field="comments", value=raw_item.get("value", raw_item)
            )
            return normalize_instagram_comment(change)

        # 3. Facebook feed change
        if raw_item.get("field") == "feed" or (
            raw_item.get("item") == "comment" and "verb" in raw_item
        ):
            change = WebhookChange(field="feed", value=raw_item.get("value", raw_item))
            return normalize_facebook_comment(change)

        # 4. Messaging item
        if "sender" in raw_item and "recipient" in raw_item:
            platform = (
                Platform.INSTAGRAM
                if str(raw_item.get("object", "")).lower() == "instagram"
                else Platform.FACEBOOK
            )
            with suppress(ValidationError, ValueError, TypeError):
                entry = WebhookEntry.model_validate(
                    {"id": "0", "messaging": [raw_item]}
                )
                if entry.messaging:
                    return normalize_messaging_item(platform, entry.messaging[0])

        # 5. Full WebhookPayload structure (extract first valid change/messaging)
        changes = raw_item.get("changes")
        if isinstance(changes, list) and changes:
            ch = changes[0]
            field = ch.get("field")
            if field == "comments":
                return normalize_instagram_comment(WebhookChange.model_validate(ch))
            if field == "feed":
                return normalize_facebook_comment(WebhookChange.model_validate(ch))

        messaging = raw_item.get("messaging")
        if isinstance(messaging, list) and messaging:
            platform = (
                Platform.INSTAGRAM
                if str(raw_item.get("object", "")).lower() == "instagram"
                else Platform.FACEBOOK
            )
            with suppress(ValidationError, ValueError, TypeError):
                item_model = WebhookEntry.model_validate(
                    {"id": "0", "messaging": messaging}
                ).messaging
                if item_model:
                    return normalize_messaging_item(platform, item_model[0])

        return None
