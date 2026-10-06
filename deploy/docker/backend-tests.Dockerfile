# Appended to the exact committed backend/Dockerfile by the local runner.
# The runtime stage remains unchanged; this image exists only for local tests.
FROM runtime AS local-backend-tests
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git strace \
    && rm -rf /var/lib/apt/lists/*
COPY . /app/source/
RUN mv /app/source/.ci-source-git /app/source/.git \
    && uv sync --frozen --project /app/source/backend --group dev --no-editable \
    && chown -R trackvance:trackvance /app/source
ENV PATH="/app/source/backend/.venv/bin:$PATH" \
    TRACKVANCE_BACKEND_DIR=/app/source/backend \
    PYTHONPATH=/app/source/scripts
WORKDIR /app/source/backend
USER trackvance
CMD ["sleep", "infinity"]
