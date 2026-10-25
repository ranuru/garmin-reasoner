# syntax=docker/dockerfile:1
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 app \
    && mkdir -p /app/data /app/backups \
    && chown app /app/data /app/backups
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    DATABASE_PATH=/app/data/garmin.db \
    BACKUP_DIR=/app/backups

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
CMD ["uvicorn", "garmin_reasoner.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
