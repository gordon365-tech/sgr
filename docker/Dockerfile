FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pip==24.0 hatchling

COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install ".[ml]"

COPY . .
RUN pip install --no-cache-dir --prefix=/install -e .

# ---------------------------------------------------------------------------
# Runtime image (minimal)
# ---------------------------------------------------------------------------
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /build/sgr ./sgr

# Non-root user for security
RUN useradd -m -u 1000 sgr && chown -R sgr:sgr /app
USER sgr

EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# CRITICAL: MarketDataEngine and TradingOrchestrator run in lifespan.
# Multiple workers = multiple market subscriptions + duplicate trading cycles.
# Until API and worker are physically separated: --workers 1 only.
CMD ["uvicorn", "sgr.api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log"]
