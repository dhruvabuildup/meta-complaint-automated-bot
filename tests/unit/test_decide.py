"""Unit tests for comment intent matching, complaint handoff, and reply composition."""

from datetime import datetime, timezone

import pytest

from meta_bot.config import Settings
from meta_bot.domain.enums import HandoffReason, IntentGroup, Platform
from meta_bot.domain.models import CommentEvent
from meta_bot.services.decide import (
    PUBLIC_REPLY_VARIANTS,
    CommentDecisionService,
    classify_comment_intent,
    compose_private_reply,
    select_public_reply,
)


@pytest.mark.parametrize(
    "text,expected_intent,expected_handoff",
    [
        ("How much does this cost?", IntentGroup.PRICE, False),
        ("What is the price of the blue one?", IntentGroup.PRICE, False),
        ("Where can I buy this? Send link please", IntentGroup.LINK, False),
        ("Do you have a website catalog?", IntentGroup.LINK, False),
        ("Where are you located in the city?", IntentGroup.LOCATION, False),
        ("Can I visit your store tomorrow?", IntentGroup.LOCATION, False),
        ("What are your business hours?", IntentGroup.INFO, False),
        ("Tell me more specs and details", IntentGroup.INFO, False),
        ("Love this design, looks awesome!", IntentGroup.GENERAL, False),
    ],
)
def test_intent_classification(
    text: str,
    expected_intent: IntentGroup,
    expected_handoff: bool,
) -> None:
    """Validate intent matching across configured keyword groups."""
    intent, is_handoff, reason = classify_comment_intent(text)
    assert intent == expected_intent
    assert is_handoff is expected_handoff
    assert reason is None


@pytest.mark.parametrize(
    "text",
    [
        "I demand an immediate refund for my order!",
        "This company is a scam and total fraud",
        "My item arrived broken and damaged",
        "Worst experience ever, terrible quality",
        "I will sue you, talking to my lawyer today",
        "Unacceptable service, I want my money back",
        "Dangerous and unsafe product",
    ],
)
def test_complaints_and_sensitive_words_trigger_human_handoff(text: str) -> None:
    """Meta rule: Complaints, refunds, anger, and legal threats NEVER auto-reply."""
    intent, is_handoff, reason = classify_comment_intent(text)
    assert intent == IntentGroup.COMPLAINT
    assert is_handoff is True
    assert reason == HandoffReason.COMPLAINT


def test_private_reply_mandatory_disclosures(test_settings: Settings) -> None:
    """Meta rule: First DM must disclose bot identity, call to action, human contact, and STOP."""
    dm_text = compose_private_reply(
        intent=IntentGroup.PRICE,
        account_name="Acme Goods",
        human_contact=test_settings.DEFAULT_HUMAN_CONTACT,
    )

    # 1. Automated assistant disclosure
    assert "automated assistant for Acme Goods" in dm_text
    # 2. Call to action / pricing info
    assert "pricing options" in dm_text
    # 3. Easy human contact
    assert "reply HUMAN or email us at support@example.com" in dm_text
    # 4. Mandatory opt-out disclosure
    assert "Reply STOP to opt out." in dm_text


def test_public_reply_rotation_never_repeats_last_variant() -> None:
    """Meta rule: Never post identical public replies repeatedly on the same post."""
    pool = PUBLIC_REPLY_VARIANTS[IntentGroup.PRICE]
    assert len(pool) >= 5

    last_variant = pool[0]

    # When last_variant is provided, selector picks another candidate
    selected = select_public_reply(
        intent=IntentGroup.PRICE,
        last_variant=last_variant,
        ratio=1.0,
        rng_val=0.1,
    )
    assert selected is not None
    assert selected != last_variant


def test_public_reply_ratio_filtering() -> None:
    """Public replies are only scheduled when random roll is below configured ratio."""
    # When roll exceeds ratio (0.8 > 0.3), no public reply is returned
    selected = select_public_reply(
        intent=IntentGroup.GENERAL,
        ratio=0.3,
        rng_val=0.8,
    )
    assert selected is None

    # When roll is under ratio (0.1 <= 0.3), reply is chosen
    selected = select_public_reply(
        intent=IntentGroup.GENERAL,
        ratio=0.3,
        rng_val=0.1,
    )
    assert selected is not None


def test_decision_service_handles_complaint_with_zero_sends(
    test_settings: Settings,
) -> None:
    """Decision service outputs plan with zero sends for complaint comments."""
    service = CommentDecisionService(settings=test_settings)

    event = CommentEvent(
        platform=Platform.INSTAGRAM,
        comment_id="ig_c_123",
        post_id="ig_p_456",
        commenter_id="user_angry",
        text="This product is broken! I want a full refund!",
        created_at=datetime.now(timezone.utc),
    )

    plan = service.decide(event=event)
    assert plan.is_handed_off is True
    assert plan.handoff_reason == HandoffReason.COMPLAINT
    assert plan.public_reply_text is None
    assert plan.private_reply_text is None


def test_decision_service_normal_comment_generates_both_replies(
    test_settings: Settings,
) -> None:
    """Decision service outputs both public and private replies for typical inquiry."""
    # Force public reply by injecting constant 0.05
    service = CommentDecisionService(
        settings=test_settings,
        random_source=lambda: 0.05,
    )

    event = CommentEvent(
        platform=Platform.INSTAGRAM,
        comment_id="ig_c_789",
        post_id="ig_p_456",
        commenter_id="user_buyer",
        text="How much is this item?",
        created_at=datetime.now(timezone.utc),
    )

    plan = service.decide(event=event)
    assert plan.is_handed_off is False
    assert plan.intent_group == IntentGroup.PRICE
    assert plan.public_reply_text is not None
    assert plan.private_reply_text is not None
    assert "automated assistant" in plan.private_reply_text
    assert "Reply STOP to opt out" in plan.private_reply_text
