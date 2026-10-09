"""Table-driven unit tests for the 8-step event filtering pipeline."""

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from meta_bot.config import Settings
from meta_bot.domain.enums import DecisionKind, DropReason, Platform
from meta_bot.domain.models import Clock, CommentEvent
from meta_bot.services.pipeline import EventPipeline
from meta_bot.services.ports import (
    ActionRepository,
    CommentRepositoryPort,
    ContactRepositoryPort,
    DedupePort,
)


class FakeDedupe(DedupePort):
    """In-memory atomic dedupe port using a thread-safe set."""

    def __init__(self, pre_existing_keys: set[str] | None = None) -> None:
        self.keys: set[str] = set(pre_existing_keys or ())
        self._lock = asyncio.Lock()

    async def set_nx(self, key: str, value: str, ttl_seconds: int = 604800) -> bool:
        async with self._lock:
            if key in self.keys:
                return False
            self.keys.add(key)
            return True


class FakeContactRepo(ContactRepositoryPort):
    """In-memory contact repository with opt-out lookup."""

    def __init__(self, opted_out_ids: set[str] | None = None) -> None:
        self.opted_out_ids = set(opted_out_ids or ())
        self.contacts: dict[str, UUID] = {}

    async def get_or_create_contact(
        self,
        platform: Platform,
        external_id: str,
        username: str | None = None,
    ) -> tuple[UUID, bool]:
        if external_id not in self.contacts:
            self.contacts[external_id] = uuid4()
        return (self.contacts[external_id], external_id in self.opted_out_ids)


class FakeCommentRepo(CommentRepositoryPort):
    """In-memory comment repository returning UUIDs."""

    def __init__(self) -> None:
        self.comments: dict[str, UUID] = {}

    async def upsert_comment(self, comment: CommentEvent) -> UUID:
        if comment.comment_id not in self.comments:
            self.comments[comment.comment_id] = uuid4()
        return self.comments[comment.comment_id]


class FakeActionRepo(ActionRepository):
    """In-memory action repository tracking claimed private replies."""

    def __init__(self, already_claimed: set[UUID] | None = None) -> None:
        self.claimed: set[UUID] = set(already_claimed or ())
        self._lock = asyncio.Lock()

    async def claim_private_reply(
        self,
        platform: Platform,
        contact_id: UUID,
        comment_id: UUID,
        body_hash: str,
    ) -> bool:
        async with self._lock:
            if comment_id in self.claimed:
                return False
            self.claimed.add(comment_id)
            return True


class FakeClock(Clock):
    """Deterministic clock returning fixed timestamp."""

    def __init__(self, fixed_now: datetime | None = None) -> None:
        self._now = fixed_now or datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now


def _valid_ig_comment_payload(
    comment_id: str = "c_1001",
    author_id: str = "u_9999",
    text: str = "How much does shipping cost?",
    parent_id: str | None = None,
) -> dict[str, Any]:
    val: dict[str, Any] = {
        "id": comment_id,
        "text": text,
        "from": {"id": author_id, "username": "shopper"},
        "media": {"id": "media_555"},
    }
    if parent_id:
        val["parent_id"] = parent_id
    return {
        "field": "comments",
        "value": val,
    }


def _valid_fb_comment_payload(
    comment_id: str = "fb_c_2001",
    author_id: str = "fb_u_8888",
    text: str = "Is this still available in size M?",
    verb: str = "add",
    parent_id: str | None = None,
) -> dict[str, Any]:
    val: dict[str, Any] = {
        "item": "comment",
        "verb": verb,
        "comment_id": comment_id,
        "post_id": "post_777",
        "message": text,
        "from": {"id": author_id, "name": "FB Shopper"},
        "created_time": 1712000000,
    }
    if parent_id:
        val["parent_id"] = parent_id
    return {
        "field": "feed",
        "value": val,
    }


def _valid_ig_dm_payload(
    mid: str = "mid_3001",
    sender_id: str = "u_dm_111",
    text: str = "Hello there",
    is_echo: bool = False,
) -> dict[str, Any]:
    return {
        "object": "instagram",
        "sender": {"id": sender_id},
        "recipient": {"id": "17841400000000001"},
        "message": {"mid": mid, "text": text, "is_echo": is_echo},
    }


@pytest.mark.asyncio
async def test_filter_1_edit_or_delete_ignored(test_settings: Settings) -> None:
    """Step 1: Edits, removes, and deletions are dropped immediately."""
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=test_settings,
    )

    # Facebook feed edit
    edit_item = _valid_fb_comment_payload(verb="edited")
    decision = await pipeline.process_event(edit_item)
    assert decision.kind == DecisionKind.DROP
    assert decision.drop_reason == DropReason.EDIT_OR_DELETE

    # Facebook feed remove
    remove_item = _valid_fb_comment_payload(verb="remove")
    decision_remove = await pipeline.process_event(remove_item)
    assert decision_remove.kind == DecisionKind.DROP
    assert decision_remove.drop_reason == DropReason.EDIT_OR_DELETE


@pytest.mark.asyncio
async def test_filter_1_unsupported_event(test_settings: Settings) -> None:
    """Step 1: Unknown or unparseable event payloads are dropped."""
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=test_settings,
    )
    decision = await pipeline.process_event({"unknown_field": "unknown_val"})
    assert decision.kind == DecisionKind.DROP
    assert decision.drop_reason == DropReason.UNSUPPORTED_EVENT


@pytest.mark.asyncio
async def test_filter_2_own_comment_or_echo_ignored(test_settings: Settings) -> None:
    """Step 2: Comments authored by our own Page/IG account or DM echoes are dropped."""
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=test_settings,
    )

    # Own IG comment
    own_ig = _valid_ig_comment_payload(author_id=test_settings.IG_ACCOUNT_ID)
    decision_ig = await pipeline.process_event(own_ig)
    assert decision_ig.kind == DecisionKind.DROP
    assert decision_ig.drop_reason == DropReason.OWN_EVENT

    # Own FB comment
    own_fb = _valid_fb_comment_payload(author_id=test_settings.PAGE_ID)
    decision_fb = await pipeline.process_event(own_fb)
    assert decision_fb.kind == DecisionKind.DROP
    assert decision_fb.drop_reason == DropReason.OWN_EVENT

    # Message echo
    echo_dm = _valid_ig_dm_payload(is_echo=True)
    decision_echo = await pipeline.process_event(echo_dm)
    assert decision_echo.kind == DecisionKind.DROP
    assert decision_echo.drop_reason == DropReason.OWN_EVENT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spam_text",
    [
        "Contact me on whatsapp +123456789",
        "Join my telegram channel t.me/profit",
        "Best crypto investment opportunity",
        "Make 1000 daily with forex",
        "DM me to earn fast cash",
    ],
)
async def test_filter_3_spam_pattern_ignored(
    spam_text: str, test_settings: Settings
) -> None:
    """Step 3: Comments matching spam or promotional keyword patterns are dropped."""
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=test_settings,
    )
    spam_event = _valid_ig_comment_payload(text=spam_text)
    decision = await pipeline.process_event(spam_event)
    assert decision.kind == DecisionKind.DROP
    assert decision.drop_reason == DropReason.SPAM_OR_PAGE


@pytest.mark.asyncio
async def test_filter_4_dedupe_duplicate_dropped(test_settings: Settings) -> None:
    """Step 4: Repeated deliveries of same comment or mid are dropped via atomic Redis dedupe."""
    dedupe = FakeDedupe()
    pipeline = EventPipeline(
        dedupe_port=dedupe,
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=test_settings,
    )

    event_payload = _valid_ig_comment_payload(comment_id="c_dedupe_999")
    # First delivery succeeds
    decision1 = await pipeline.process_event(event_payload)
    assert decision1.kind == DecisionKind.PROCEED

    # Second delivery (duplicate retry) is dropped
    decision2 = await pipeline.process_event(event_payload)
    assert decision2.kind == DecisionKind.DROP
    assert decision2.drop_reason == DropReason.DUPLICATE


@pytest.mark.asyncio
async def test_filter_5_top_level_gating(test_settings: Settings) -> None:
    """Step 5: Threaded comment replies dropped when ALLOW_REPLIES_TO_REPLIES is False."""
    settings_no_replies = test_settings.model_copy(
        update={"ALLOW_REPLIES_TO_REPLIES": False}
    )
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=settings_no_replies,
    )

    threaded_comment = _valid_ig_comment_payload(parent_id="parent_root_123")
    decision = await pipeline.process_event(threaded_comment)
    assert decision.kind == DecisionKind.DROP
    assert decision.drop_reason == DropReason.THREADED_REPLY_IGNORED

    # Allowed when ALLOW_REPLIES_TO_REPLIES is True
    settings_allow_replies = test_settings.model_copy(
        update={"ALLOW_REPLIES_TO_REPLIES": True}
    )
    pipeline_allow = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=settings_allow_replies,
    )
    decision_allowed = await pipeline_allow.process_event(threaded_comment)
    assert decision_allowed.kind == DecisionKind.PROCEED


@pytest.mark.asyncio
async def test_filter_6_atomic_claim_private_reply(test_settings: Settings) -> None:
    """Step 6: Exactly one private reply per comment. Second claim fails."""
    action_repo = FakeActionRepo()
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=action_repo,
        clock=FakeClock(),
        settings=test_settings,
    )

    event_payload = _valid_ig_comment_payload(comment_id="c_atomic_claim_1")
    # Bypass dedupe to simulate a collision reaching claim level
    decision1 = await pipeline.process_event(event_payload)
    assert decision1.kind == DecisionKind.PROCEED

    # Re-run on pipeline with cleared dedupe but same action_repo
    pipeline2 = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=FakeContactRepo(),
        comment_repo=pipeline.comment_repo,
        action_repo=action_repo,
        clock=FakeClock(),
        settings=test_settings,
    )
    decision2 = await pipeline2.process_event(event_payload)
    assert decision2.kind == DecisionKind.DROP
    assert decision2.drop_reason == DropReason.PRIVATE_REPLY_ALREADY_CLAIMED


@pytest.mark.asyncio
async def test_filter_7_contact_opted_out(test_settings: Settings) -> None:
    """Step 7: Opted-out contact comments or DMs are dropped."""
    contact_repo = FakeContactRepo(opted_out_ids={"user_opted_out_777"})
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(),
        contact_repo=contact_repo,
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=test_settings,
    )

    # Opted-out comment
    opted_comment = _valid_ig_comment_payload(author_id="user_opted_out_777")
    decision_comment = await pipeline.process_event(opted_comment)
    assert decision_comment.kind == DecisionKind.DROP
    assert decision_comment.drop_reason == DropReason.CONTACT_OPTED_OUT

    # Opted-out DM
    opted_dm = _valid_ig_dm_payload(sender_id="user_opted_out_777")
    decision_dm = await pipeline.process_event(opted_dm)
    assert decision_dm.kind == DecisionKind.DROP
    assert decision_dm.drop_reason == DropReason.CONTACT_OPTED_OUT


@pytest.mark.asyncio
async def test_filter_order_evaluation(test_settings: Settings) -> None:
    """Verify filters evaluate in strict priority order."""
    pipeline = EventPipeline(
        dedupe_port=FakeDedupe(pre_existing_keys={"mb:v1:dedupe:instagram:c_order"}),
        contact_repo=FakeContactRepo(),
        comment_repo=FakeCommentRepo(),
        action_repo=FakeActionRepo(),
        clock=FakeClock(),
        settings=test_settings,
    )

    # Own comment + duplicate: Filter 2 (own) must win before Filter 4 (dedupe)
    own_and_dup = _valid_ig_comment_payload(
        comment_id="c_order",
        author_id=test_settings.IG_ACCOUNT_ID,
    )
    decision_own = await pipeline.process_event(own_and_dup)
    assert decision_own.drop_reason == DropReason.OWN_EVENT

    # Spam + duplicate: Filter 3 (spam) must win before Filter 4 (dedupe)
    spam_and_dup = _valid_ig_comment_payload(
        comment_id="c_order",
        text="Check my crypto investment",
    )
    decision_spam = await pipeline.process_event(spam_and_dup)
    assert decision_spam.drop_reason == DropReason.SPAM_OR_PAGE


@pytest.mark.asyncio
async def test_race_condition_concurrent_events(test_settings: Settings) -> None:
    """Test race safety: concurrent duplicate events yield exactly 1 PROCEED and N-1 DROPs."""
    dedupe = FakeDedupe()
    action_repo = FakeActionRepo()
    contact_repo = FakeContactRepo()
    comment_repo = FakeCommentRepo()

    pipeline = EventPipeline(
        dedupe_port=dedupe,
        contact_repo=contact_repo,
        comment_repo=comment_repo,
        action_repo=action_repo,
        clock=FakeClock(),
        settings=test_settings,
    )

    event_payload = _valid_ig_comment_payload(comment_id="c_race_concurrent_001")

    # Launch 10 concurrent process_event tasks
    results = await asyncio.gather(
        *[pipeline.process_event(event_payload) for _ in range(10)]
    )

    proceed_count = sum(1 for r in results if r.kind == DecisionKind.PROCEED)
    drop_count = sum(1 for r in results if r.kind == DecisionKind.DROP)

    assert proceed_count == 1, f"Expected exactly 1 PROCEED, got {proceed_count}"
    assert drop_count == 9, f"Expected 9 DROPs, got {drop_count}"
    for r in results:
        if r.kind == DecisionKind.DROP:
            assert r.drop_reason in (
                DropReason.DUPLICATE,
                DropReason.PRIVATE_REPLY_ALREADY_CLAIMED,
            )
