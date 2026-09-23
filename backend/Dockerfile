FROM ghcr.io/astral-sh/uv:0.12.13 AS uv
FROM python:3.12-slim

COPY --from=uv /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    TRACKVANCE_BACKEND_DIR=/app/backend \
    TRACKVANCE_STORAGE_DIR=/var/lib/trackvance \
    PATH="/app/backend/.venv/bin:$PATH"

WORKDIR /app/backend

# The committed lock controls dependency versions for the API and worker.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
COPY demo/ /app/demo/
RUN uv sync --frozen --no-dev --no-editable \
    && groupadd --system trackvance \
    && useradd --system --gid trackvance --home-dir /app trackvance \
    && mkdir -p /var/lib/trackvance /var/lib/trackvance-credentials /var/lib/trackvance-keys \
        /var/lib/trackvance-delivery-credentials /var/lib/trackvance-delivery-keys \
    && chown -R trackvance:trackvance /var/lib/trackvance /var/lib/trackvance-credentials \
        /var/lib/trackvance-keys /var/lib/trackvance-delivery-credentials \
        /var/lib/trackvance-delivery-keys \
    && chmod 700 /var/lib/trackvance-credentials /var/lib/trackvance-keys \
        /var/lib/trackvance-delivery-credentials /var/lib/trackvance-delivery-keys

USER trackvance
EXPOSE 8000

# Compose overrides this command with `python -m trackvance.worker` for the worker.
CMD ["python", "-m", "uvicorn", "trackvance.api:app", "--host", "0.0.0.0", "--port", "8000"]
