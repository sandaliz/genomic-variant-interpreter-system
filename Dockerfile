# VariantLens — FastAPI runtime image.
#
# Installs only the runtime dependency set (requirements-api.txt), which leaves
# out the notebook/analysis stack the server never imports.
#
# The app needs DATABASE_URL plus the two JWT secrets at run time. None are baked
# into the image (see .env.example); pass them in with `--env-file` or the
# platform's secret store. Run `alembic upgrade head` separately, not on boot, so
# a rolling deploy never races a migration.
#
#   docker build -t variantlens .
#   docker run --rm -p 8000:8000 --env-file .env variantlens

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# psycopg[binary] ships its own libpq, so no build toolchain is required here.
COPY requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
