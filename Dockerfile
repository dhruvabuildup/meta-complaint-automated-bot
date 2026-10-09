# Multi-stage Dockerfile for Meta comment & DM automation bot
# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Install compilation dependencies if needed
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Final minimal runtime image
FROM python:3.12-slim AS runner

WORKDIR /app

# Install runtime libpq for PostgreSQL
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Create unprivileged application user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/false -m appuser

# Copy installed Python packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Set environment paths
ENV PATH="/home/appuser/.local/bin:$PATH" \
    PYTHONPATH="/app/src:$PYTHONPATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy application source code
COPY --chown=appuser:appgroup src ./src

# Switch to unprivileged user
USER appuser

EXPOSE 8000

# Default entrypoint runs the FastAPI ASGI application
CMD ["uvicorn", "meta_bot.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
