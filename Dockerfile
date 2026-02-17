# Stage 1: Build
FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install \
    fastapi "uvicorn[standard]" ib_async \
    sqlalchemy aiosqlite greenlet httpx python-telegram-bot \
    pydantic-settings python-dotenv
COPY app/ ./app/

# Stage 2: Runtime
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY --from=builder /build/app ./app

RUN mkdir -p /data && useradd -m appuser && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8001

ENV PYTHONUNBUFFERED=1
ENV DATABASE_URL="sqlite+aiosqlite:////data/trades.db"

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
