# Project: Meta comment and DM automation bot

## What we are building
An in-house service for ONE Facebook Page and its linked Instagram professional account. About 90% of the business's customers come from Meta ads, so the Page, Instagram account and ad account are a protected asset. A restriction by Meta would hurt the whole business. The service must:
1. Receive comment events (Instagram and Facebook, including ad and boosted posts) through Meta webhooks.
2. Reply publicly (varied wording, only when useful) and send exactly one private reply (DM) per commenter.
3. Hold two-way automated DM chat on Instagram and Facebook Messenger after the person replies or messages us, answering only from approved business facts, and handing off to a human when needed.
4. Stay inside Meta's rules, never use scraping or browser automation, and be able to stop instantly (kill switch).
We are NOT using Meta Business Agent, WhatsApp, GoHighLevel, ManyChat or any third-party automation. Official Meta Graph API only.

## Language and stack
- Python 3.12, FastAPI (ASGI), uvicorn, pydantic v2 and pydantic-settings, SQLAlchemy 2.0 (async) with asyncpg and Alembic, redis.asyncio, httpx (async), structlog, tenacity (retries).
- Tooling: uv or pip-tools for dependencies requirements.txt (uv/pip install), ruff (lint and format), mypy in strict mode, pytest, pytest-asyncio, respx (mock httpx), fakeredis, pre-commit, gitleaks.
- Docker and docker-compose for local run (app, worker, postgres, redis). Caddy or a tunnel is added later, not now.

## Clean code rules (non-negotiable)
- Layered, ports-and-adapters architecture. Dependencies point inward: api -> services -> domain; adapters (Meta, LLM, DB, Redis) implement interfaces (typing.Protocol) defined by the services. Domain code never imports FastAPI, SQLAlchemy, httpx or Redis.
- Full type hints everywhere. mypy strict must pass. No `Any` unless justified with a comment.
- Small functions (one job, ideally under 25 lines), small modules, no god classes, no circular imports. Prefer composition and dependency injection over globals and singletons.
- No magic numbers or strings: all tunables live in `Settings` (pydantic-settings) or named constants/enums.
- Use enums and frozen dataclasses or pydantic models for domain objects (Platform, ChannelKind, ConversationState, SendKind, etc.). No raw dicts crossing layer boundaries: parse Meta payloads into typed models at the edge.
- Errors: define a small exception hierarchy (MetaApiError with subclasses for auth, rate limit, permission, window-expired, transient, permanent). Never swallow exceptions silently. Never use bare `except`.
- Logging: structlog JSON logs with a correlation id per event. NEVER log tokens, app secret, full message text of customers, or personal data beyond ids. Redact by default.
- Docstrings on public functions explain WHY and any Meta rule being enforced. Comments reference the rule (for example "Meta: one private reply per commenter within 7 days").
- Every module has unit tests. Business rules (filters, caps, windows, state machine, output checks) need table-driven tests. No real network calls in tests: mock Meta with respx, Redis with fakeredis.
- Idempotency everywhere: webhook handling and sends must be safe to run twice.
- Never invent Meta API shapes. If a payload field, endpoint, parameter or error code is not certain, put `# TODO(verify): <what to check in Meta docs or App Dashboard Test button>` and make it configurable. Store real payload samples as JSON fixtures under tests/fixtures/ once we capture them.

## Meta rules the code must enforce (do not break these)
- Receive comments via webhooks, never by polling. Instagram uses the `comments` field; the Facebook Page uses the `feed` field where comments arrive as item=comment, verb=add.
- Webhook GET verification uses hub.mode, hub.verify_token, hub.challenge. POST requests must have their raw body verified with HMAC-SHA256 using the App Secret against the X-Hub-Signature-256 header, using constant-time compare. Return 200 immediately and process asynchronously. Meta retries failed deliveries for up to about 36 hours and ad/boosted posts can deliver duplicates, so dedupe on comment id or message id.
- Public reply: Instagram POST /{ig-comment-id}/replies with message; Facebook POST /{comment-id}/comments with message.
- Private reply (DM from a comment): POST /{PAGE_ID}/messages with recipient.comment_id. EXACTLY ONE private reply per commenter and per comment, within 7 days of the comment. After that, further messages are allowed only after the person replies, and only within 24 hours of their last message. A human agent tag may extend to 7 days but ONLY for a human, never for bot messages.
- The bot must NEVER initiate cold or promotional DMs. The sender must refuse any send that is not (a) a private reply to a comment within 7 days or (b) a reply inside the 24-hour window.
- Bots must answer every user input within 30 seconds. There must always be a fast fixed fallback reply (used if the LLM is slow or the kill switch is on).
- Non-followers' private replies land in the Requests folder, expect lower visibility. No one-time notifications or sponsored messages on Instagram.
- Meta's ceiling is 750 private replies per hour per Instagram account. WE run far lower: defaults 100 per hour and 1,000 per day, configurable, sends paced with random 20 to 60 second delay for first DMs. Meta does not publish spam thresholds, so start low.
- Never post identical public replies repeatedly. Use a pool of variants and cap duplicates. Reply publicly only when it adds value.
- Disclose that it is an automated assistant in the first DM, include a way to reach a human and a STOP/opt-out. Honour STOP before any other logic.
- Complaints, refunds, legal, safety, payment details, anger, and low confidence go to a human, never to auto-reply.
- Meta permissions in play (Facebook Login for Business path): instagram_basic, instagram_manage_comments, instagram_manage_messages, pages_read_engagement, pages_messaging, pages_manage_metadata, pages_show_list, pages_manage_engagement, and later ads_read and ads_management for ad comments. Graph API version is pinned in config, never hardcoded in code.

## Git workflow
- Conventional Commits (feat:, fix:, refactor:, test:, docs:, chore:). Small, focused commits.
- One branch per prompt (for example feat/01-foundation). Open a PR description summarising changes, test results, and any TODO(verify) items. Never commit to main directly after Prompt 1.
- Never commit .env, tokens, secrets, or real customer data. `.env.example` has placeholders only.

## Definition of done for every task
`ruff check`, `ruff format --check`, `mypy --strict` and `pytest` all pass; new behaviour has tests; docs/README updated; TODO(verify) items listed in the PR description.