#!/usr/bin/env python3
"""Seed starter reply templates into the database for testing and local development."""

import asyncio
import os
import sys

# Ensure src is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy.dialects.postgresql import insert

from meta_bot.config import get_settings
from meta_bot.domain.enums import IntentGroup, Platform
from meta_bot.infra.db.engine import create_engine
from meta_bot.infra.db.models import TemplateModel
from meta_bot.infra.db.session import create_session_factory
from meta_bot.services.decide import INTENT_CORE_MESSAGES, PUBLIC_REPLY_VARIANTS


async def seed() -> None:
    """Insert default template records into the templates table."""
    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)
    session_factory = create_session_factory(engine)

    print("Seeding reply templates...")
    async with session_factory() as session:
        for platform in (Platform.INSTAGRAM, Platform.FACEBOOK):
            for intent in IntentGroup:
                if intent == IntentGroup.COMPLAINT:
                    continue

                variants = PUBLIC_REPLY_VARIANTS.get(intent, [])
                body = INTENT_CORE_MESSAGES.get(intent, "")

                stmt = insert(TemplateModel).values(
                    kind=intent.value,
                    platform=platform.value,
                    language="en",
                    body=body,
                    active=True,
                    version=1,
                    variants=variants,
                )
                await session.execute(stmt)

        await session.commit()
    await engine.dispose()
    print("Templates successfully seeded!")


if __name__ == "__main__":
    asyncio.run(seed())
