# VariantLens API image.
#
#   docker build -t variantlens-api .
#   docker run -p 8000:8000 --env-file .env variantlens-api
#
# The database is NOT part of this image: in Compose the `db` service provides
# PostgreSQL, and in a real deployment it is a managed instance reached through
# DATABASE_URL.

FROM python:3.13-slim

# Fail fast, no .pyc clutter, and no interactive prompts from apt.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is only here for the health check below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first: this layer is cached until requirements actually change,
# so editing application code does not reinstall pandas and scikit-learn.
COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY . .

# Run as a non-root user. A container process that can rewrite its own code is a
# needless escalation path if anything else in the stack is compromised.
RUN useradd --create-home --shell /usr/bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# /healthz is the same endpoint Render and most orchestrators poll.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# Migrations run inline, then uvicorn takes over as PID 1 so SIGTERM reaches it
# directly and the container stops promptly. Running them as a separate Compose
# service instead would race if a second replica started at the same moment.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]