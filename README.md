# Meta Comment & DM Automation Bot

In-house automation service for **ONE Facebook Page** and its linked **Instagram Professional Account**. Built exclusively using the official Meta Graph API (no ManyChat, GoHighLevel, or browser automation).

Because ~90% of business customers originate from Meta ads, the Page, Instagram account, and ad account are mission-critical assets. This bot is engineered to strictly uphold Meta Platform Rules and Messenger Policies.

---

## Architecture Overview

The system follows a **Ports-and-Adapters (Hexagonal)** architecture where dependencies point strictly inward:

```
┌─────────────────────────────────────────────────────────────┐
│                       Delivery Layer                        │
│             FastAPI (HTTP / Webhooks / Health)              │
│             Background Workers (Asyncio / Queues)           │
└──────────────┬───────────────────────────────┬──────────────┘
               │                               │
               ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    Application / Services                   │
│             Ports & Interfaces (typing.Protocol)            │
└──────────────┬───────────────────────────────┬──────────────┘
               │                               │
               ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│                         Domain Layer                        │
│       Pure Python (Enums, Frozen Models, Policy Rules)      │
│       Zero I/O, No Database, No Framework Dependencies      │
└─────────────────────────────────────────────────────────────┘
               ▲                               ▲
               │                               │
┌──────────────┴───────────────────────────────┴──────────────┐
│                    Infrastructure Adapters                  │
│   Meta Graph API Client (httpx, tenacity)                   │
│   PostgreSQL Storage (SQLAlchemy 2.0 async, asyncpg)        │
│   Redis (Caching, Rate Limiting, Deduplication, Kill Switch)│
└─────────────────────────────────────────────────────────────┘
```

### Core Meta Rules Enforced by Design

- **Receive via Webhooks only**: No polling. Deduplicate on comment ID and message ID.
- **Private Reply Policy**: Exactly ONE private reply per comment, within 7 days.
- **Messaging Window**: 24-hour customer service window for subsequent bot DMs. Never initiate cold or promotional DMs.
- **Volume & Pacing**: Conservative rate limits (defaults: 100/hr, 1,000/day; Meta maximum ceiling is 750/hr). Jittered delays (20–60s) for first DMs.
- **Latency & Fallback**: Fast fallback response triggered if LLM exceeds budget (Meta expects replies < 30s).
- **Kill Switch**: Instant global kill switch backed by Redis halts all outbound actions.
- **Compliance & Opt-out**: Immediate honor of STOP/opt-out, disclosures on first DM, and automatic escalation of complaints to human agents.

---

## Environment Variables

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `APP_ENV` | `local \| staging \| production` | `local` | Application runtime environment |
| `LOG_LEVEL` | `str` | `INFO` | Structlog level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `APP_ID` | `str` | *Required* | Meta Facebook App ID |
| `APP_SECRET` | `SecretStr` | *Required* | Facebook App Secret (used for HMAC verification) |
| `VERIFY_TOKEN` | `SecretStr` | *Required* | Webhook verification token |
| `GRAPH_VERSION` | `str` | `v25.0` | Pinned Graph API version prefix |
| `GRAPH_BASE_URL` | `str` | `https://graph.facebook.com` | Base URL for Graph API |
| `PAGE_ID` | `str` | *Required* | Numeric ID of the managed Facebook Page |
| `IG_ACCOUNT_ID` | `str` | *Required* | Connected Instagram Professional Account ID |
| `PAGE_ACCESS_TOKEN` | `SecretStr` | `None` | Page access token (optional at boot in local dev) |
| `DATABASE_URL` | `str` | *Required* | PostgreSQL asyncpg URL (`postgresql+asyncpg://...`) |
| `REDIS_URL` | `str` | *Required* | Redis connection URL (`redis://...`) |
| `TOKEN_ENC_KEY` | `SecretStr` | *Required* | Fernet 32-byte URL-safe base64 key for encrypting tokens |
| `ADMIN_TOKEN` | `SecretStr` | *Required* | Internal admin bearer authentication token |
| `MAX_PRIVATE_PER_HOUR`| `int` (1..750) | `100` | Hourly private reply rate ceiling |
| `MAX_PRIVATE_PER_DAY` | `int` | `1000` | Daily private reply limit |
| `SEND_DELAY_MIN_SECONDS`| `int` | `20` | Minimum jitter pacing delay before sending first DM |
| `SEND_DELAY_MAX_SECONDS`| `int` | `60` | Maximum jitter pacing delay before sending first DM |
| `PUBLIC_REPLY_RATIO` | `float` (0.0..1.0) | `0.3` | Ratio of comments to reply publicly |
| `DM_WINDOW_HOURS` | `int` | `24` | Customer service messaging window |
| `PRIVATE_REPLY_WINDOW_DAYS`| `int` | `7` | Maximum age of comment allowed for private replies |
| `BOT_REPLY_BUDGET_SECONDS`| `int` | `20` | Max duration before fast fallback reply is dispatched |
| `LLM_TIMEOUT_SECONDS` | `int` | `12` | External LLM request timeout |
| `ALERT_WEBHOOK_URL` | `str \| None` | `None` | Optional Slack/Discord webhook for alerts |
| `KILL_SWITCH_DEFAULT` | `bool` | `false` | Default kill switch state on startup |
| `DEMO_MODE` | `bool` | `false` | When true, logs write actions without calling Meta API |

---

## Quickstart

### 1. Prerequisites
- Python 3.12+
- Docker and Docker Compose

### 2. Local Setup
```bash
# Clone the repository
git clone <repo-url>
cd Meta

# Create and activate virtual environment
python3 -m venv .venv
source /home/dhruva/Meta/.venv/bin/activate

# Install dependencies
make install

# Configure environment
cp .env.example .env
```

### 3. Running with Docker Compose
Start all services (FastAPI, background worker, PostgreSQL 16, Redis 7):
```bash
make up
```

Verify service health:
```bash
curl http://localhost:8000/healthz
# {"status":"ok"}

curl http://localhost:8000/readyz
# {"status":"ready","database":"healthy","redis":"healthy"}
```

Run database migrations:
```bash
make migrate
```

To stop all services:
```bash
make down
```

---

## Makefile Targets

| Target | Description |
| :--- | :--- |
| `make install` | Install all dependencies into virtual environment |
| `make lint` | Run Ruff linter checks |
| `make format` | Automatically format code using Ruff |
| `make typecheck` | Run Mypy strict type checking on `src` and `tests` |
| `make test` | Run unit test suite |
| `make test-all` | Run all tests including database integration tests |
| `make run` | Start FastAPI development server with hot reload |
| `make worker` | Run background worker process |
| `make migrate` | Apply Alembic database migrations to head |
| `make up` | Build and start Docker Compose stack |
| `make down` | Stop Docker Compose stack |

---

## Webhook Pipeline & Simulator

### Incoming Webhook Endpoints
- **GET `/webhook`**: Meta handshake verification (`hub.mode`, `hub.verify_token`, `hub.challenge`). Returns `200` with challenge text on success, `403` on token mismatch.
- **POST `/webhook`**: Receives event payloads. Verifies `X-Hub-Signature-256` HMAC-SHA256 signature BEFORE parsing JSON, records raw event to `events_raw`, enqueues to Redis queue `mb:v1:queue:events`, and returns `200` within 200 ms target.

### Pipeline Filter Sequence
The background worker consumes incoming items and runs 8 filters in strict order:
1. Supported event kind and not an edit/delete (`verb != edited/remove/hide/deleted`).
2. Ignore own comments/replies (`author_id != PAGE_ID / IG_ACCOUNT_ID`) and DM echoes.
3. Ignore comments containing spam or external promotion patterns.
4. Atomic Redis deduplication (`SET mb:v1:dedupe:... NX EX 604800` for 7 days).
5. Top-level comment policy gating (replies to replies dropped unless `ALLOW_REPLIES_TO_REPLIES=true`).
6. Atomic private reply claim in database (guarantees exactly 1 private reply per comment).
7. Respect contact opt-out (`opted_out` status check).
8. Persist comment & contact profile; yield `PROCEED` decision for outbound action.

### Webhook Simulator CLI (`scripts/simulate_webhook.py`)
To test the pipeline locally without waiting for Meta webhook deliveries, use the simulator script:

```bash
# 1. Simulate an Instagram comment
python scripts/simulate_webhook.py --platform instagram --text "How much does this cost?"

# 2. Simulate duplicate delivery (tests 7-day atomic Redis deduplication)
python scripts/simulate_webhook.py --platform instagram --comment-id "c_test_123" --repeat 2

# 3. Simulate invalid HMAC signature (verifies 403 Forbidden rejection)
python scripts/simulate_webhook.py --bad-signature

# 4. Simulate a Facebook comment
python scripts/simulate_webhook.py --platform facebook --text "Is size medium in stock?"

# 5. Simulate an Instagram Direct Message
python scripts/simulate_webhook.py --platform instagram --event-type message --text "Hello"
```

---

## Contributing & Code Standards

- **Conventional Commits**: Format commit messages as `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, or `chore:`.
- **Zero Secrets**: Never commit real access tokens or secrets. Gitleaks runs in CI.
- **Definition of Done**:
  - `make lint` passes with 0 warnings.
  - `make typecheck` passes in Mypy strict mode.
  - `make test` passes with table-driven tests for business logic.
  - All Meta API assumptions annotated with `# TODO(verify): ...`.
