"""Decision layer for incoming comments: intent matching, escalation, and reply generation."""

import random
import re
from collections.abc import Callable

from meta_bot.config import Settings, get_settings
from meta_bot.domain.enums import HandoffReason, IntentGroup
from meta_bot.domain.models import CommentEvent, DecisionPlan

# Complaints, refunds, anger, legal, safety keywords
# Meta rule: Complaints, refunds, legal, safety, anger go to a human, NEVER to auto-reply.
COMPLAINT_PATTERNS: tuple[str, ...] = (
    r"\bcomplaint\b",
    r"\bcomplain\b",
    r"\bterrible\b",
    r"\bhorrible\b",
    r"\bawful\b",
    r"\bworst\b",
    r"\bscam\b",
    r"\bfraud\b",
    r"\bcheat\b",
    r"\bcheated\b",
    r"\bfake\b",
    r"\bstolen\b",
    r"\brefund\b",
    r"\bmoney\s+back\b",
    r"\bcancel\s+order\b",
    r"\bsue\b",
    r"\blawyer\b",
    r"\battorney\b",
    r"\blegal\b",
    r"\bpolice\b",
    r"\bcourt\b",
    r"\bbroken\b",
    r"\bdamaged\b",
    r"\bdanger\b",
    r"\bunsafe\b",
    r"\binjury\b",
    r"\bhate\b",
    r"\bdisgusted\b",
    r"\bunacceptable\b",
)

# Intent keyword groups
INTENT_PATTERNS: dict[IntentGroup, tuple[str, ...]] = {
    IntentGroup.PRICE: (
        r"\bprice\b",
        r"\bpricing\b",
        r"\bcost\b",
        r"\bhow\s+much\b",
        r"\brate\b",
        r"\bfee\b",
        r"\bfees\b",
        r"\bquote\b",
        r"\bdiscount\b",
        r"\bexpensive\b",
        r"\bcheap\b",
    ),
    IntentGroup.LINK: (
        r"\blink\b",
        r"\bwebsite\b",
        r"\burl\b",
        r"\border\b",
        r"\bbuy\b",
        r"\bshop\b",
        r"\bwhere\s+to\s+buy\b",
        r"\bcatalog\b",
        r"\bpurchase\b",
    ),
    IntentGroup.LOCATION: (
        r"\bwhere\s+are\s+you\b",
        r"\blocation\b",
        r"\baddress\b",
        r"\bcity\b",
        r"\bstore\b",
        r"\bvisit\b",
        r"\bdirections\b",
        r"\bbranch\b",
        r"\bwhere\s+located\b",
    ),
    IntentGroup.INFO: (
        r"\binfo\b",
        r"\binformation\b",
        r"\bdetails\b",
        r"\bhours\b",
        r"\btimings\b",
        r"\bavailable\b",
        r"\bspecs\b",
        r"\bfeatures\b",
        r"\btell\s+me\s+more\b",
    ),
}

# Public reply pools (at least 5 distinct variants per intent group)
# Meta rule: Never post identical public replies repeatedly. Rotate variants.
PUBLIC_REPLY_VARIANTS: dict[IntentGroup, list[str]] = {
    IntentGroup.PRICE: [
        "Thanks for your interest! We've sent you a direct message with all pricing details.",
        "Just sent you a private message with our complete pricing info!",
        "We've sent the pricing overview straight to your inbox!",
        "Check your DMs! We've shared the details and pricing with you.",
        "Thanks for asking! We just dropped the pricing details into your inbox.",
    ],
    IntentGroup.LINK: [
        "We've sent the link and order details directly to your inbox!",
        "Check your DMs for the direct link!",
        "Sent you a private message with the link and catalog!",
        "We just sent you the link in your messages. Check your inbox!",
        "Direct link sent to your DMs! Let us know if you need any help.",
    ],
    IntentGroup.LOCATION: [
        "We've sent our store locations and address directly to your inbox!",
        "Check your DMs for our full address and hours!",
        "Sent you a message with our address and visit details!",
        "We've shared our location details with you in your inbox!",
        "Check your messages for our directions and contact info!",
    ],
    IntentGroup.INFO: [
        "Thanks for your question! We've sent all the details to your inbox.",
        "Just sent you a DM with the full overview and details!",
        "Check your inbox! We've shared the requested information with you.",
        "Sent you a private message with all the details you need!",
        "We just sent you the information in your DMs!",
    ],
    IntentGroup.GENERAL: [
        "Thank you for reaching out! We've sent you a message in your inbox.",
        "Thanks for connecting! Check your DMs for a quick message from us.",
        "We appreciate your comment! Sent you a private message.",
        "Check your inbox for a quick note from our team!",
        "Thanks for your support! We've reached out to your DMs.",
    ],
}

# Core message bodies for private replies by intent
INTENT_CORE_MESSAGES: dict[IntentGroup, str] = {
    IntentGroup.PRICE: (
        "Here are our pricing options: our standard packages start at $29, "
        "and custom plans are available. You can view all pricing details at https://example.com/pricing."
    ),
    IntentGroup.LINK: (
        "Here is the official link to browse and order: https://example.com/shop."
    ),
    IntentGroup.LOCATION: (
        "Our main location is at 100 Main Street, Suite 400. "
        "We are open Monday through Saturday from 9:00 AM to 6:00 PM."
    ),
    IntentGroup.INFO: (
        "Here is more information about our services: we provide full-service support, "
        "fast turnaround, and satisfaction guarantees. Learn more at https://example.com/about."
    ),
    IntentGroup.GENERAL: (
        "Thanks for reaching out! We'd love to help answer any questions you have about our products."
    ),
}


def classify_comment_intent(
    text: str,
) -> tuple[IntentGroup, bool, HandoffReason | None]:
    """Classify comment text into an intent group and detect human handoff conditions.

    Returns:
        (IntentGroup, is_handed_off, handoff_reason)
    """
    cleaned = text.lower().strip()

    # Check complaints, refunds, legal, safety first
    for pattern in COMPLAINT_PATTERNS:
        if re.search(pattern, cleaned):
            return IntentGroup.COMPLAINT, True, HandoffReason.COMPLAINT

    # Check specific business intent groups
    for intent, patterns in INTENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, cleaned):
                return intent, False, None

    # Fallback to general intent
    return IntentGroup.GENERAL, False, None


def compose_private_reply(
    intent: IntentGroup,
    account_name: str = "our team",
    human_contact: str = "support@example.com",
) -> str:
    """Compose the first DM satisfying all Meta compliance disclosure requirements.

    Meta rules enforced:
    1. Disclose that it is an automated assistant in the first DM.
    2. Deliver the requested info or single call to action.
    3. Include an easy way to reach a human agent.
    4. Honour opt-out disclosure: include 'Reply STOP to opt out'.
    """
    # 1. Assistant disclosure
    disclosure = f"Hi! I am an automated assistant for {account_name}."

    # 2. Intent-specific core message
    core_info = INTENT_CORE_MESSAGES.get(
        intent, INTENT_CORE_MESSAGES[IntentGroup.GENERAL]
    )

    # 3. Human escalation option
    escalation = (
        f"To speak directly with a human, reply HUMAN or email us at {human_contact}."
    )

    # 4. Mandatory STOP disclosure
    opt_out = "Reply STOP to opt out."

    return f"{disclosure}\n\n{core_info}\n\n{escalation}\n\n{opt_out}"


def select_public_reply(
    intent: IntentGroup,
    last_variant: str | None = None,
    ratio: float = 0.3,
    rng_val: float | None = None,
) -> str | None:
    """Select a varied public reply variant respecting the public reply ratio.

    Meta rule: Never post identical public replies repeatedly. Use a pool and rotate.
    """
    effective_rng = rng_val if rng_val is not None else random.random()
    if effective_rng > ratio:
        return None

    pool = PUBLIC_REPLY_VARIANTS.get(intent, PUBLIC_REPLY_VARIANTS[IntentGroup.GENERAL])
    candidates = [v for v in pool if v != last_variant]
    if not candidates:
        candidates = pool

    # Deterministic or random selection
    return candidates[0] if rng_val is not None else random.choice(candidates)


class CommentDecisionService:
    """Service producing structured DecisionPlan from incoming CommentEvents."""

    def __init__(
        self,
        settings: Settings | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.random_source = random_source or random.random

    def decide(
        self,
        event: CommentEvent,
        last_public_variant: str | None = None,
        account_name: str = "our team",
    ) -> DecisionPlan:
        """Evaluate a CommentEvent and generate appropriate public and private replies.

        If a complaint or refund request is detected, zero sends are planned and
        handoff to a human agent is initiated.
        """
        intent, is_handed_off, handoff_reason = classify_comment_intent(event.text)

        if is_handed_off:
            return DecisionPlan(
                comment_id=event.comment_id,
                platform=event.platform,
                intent_group=intent,
                is_handed_off=True,
                handoff_reason=handoff_reason,
                public_reply_text=None,
                private_reply_text=None,
                metadata={"reason": "complaint_or_sensitive_keyword_detected"},
            )

        # Public reply decision
        rng = self.random_source()
        pub_text = select_public_reply(
            intent=intent,
            last_variant=last_public_variant,
            ratio=self.settings.PUBLIC_REPLY_RATIO,
            rng_val=rng,
        )

        # Private reply decision
        priv_text = compose_private_reply(
            intent=intent,
            account_name=account_name,
            human_contact=self.settings.DEFAULT_HUMAN_CONTACT,
        )

        return DecisionPlan(
            comment_id=event.comment_id,
            platform=event.platform,
            intent_group=intent,
            is_handed_off=False,
            handoff_reason=None,
            public_reply_text=pub_text,
            private_reply_text=priv_text,
            metadata={"intent": intent.value},
        )
