# syntax=docker/dockerfile:1.7
# Multi-stage backend image for the Coherence Fund API + workers (prompt 67).
#
# Build:  docker build -t coherence-engine:dev .
# Run:    docker run --rm -p 8010:8010 coherence-engine:dev
#
# This image bundles the FastAPI service, the Alembic migrator, the outbox
# worker, and the scoring worker. The CMD selects the API; the worker
# entrypoints are reachable via `python -m server.fund.scoring_worker` etc.
#
# OCI labels are filled in at build time by the release workflow so the
# pushed image is traceable back to a specific git tag + commit.

# ---------------------------------------------------------------------------
# Stage 1: builder — wheel + runtime deps into a venv
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/* \
 && python -m venv "$VIRTUAL_ENV"

WORKDIR /src

# Copy only metadata first so the dep layer is reused when source changes.
COPY pyproject.toml README.md VERSION ./
COPY scripts ./scripts

# Then bring in the sources needed for the install (editable install reads
# the package layout but does not need test data).
COPY core ./core
COPY domain ./domain
COPY layers ./layers
COPY embeddings ./embeddings
COPY server ./server
COPY data ./data
COPY config.py cli.py __init__.py __main__.py ./

RUN pip install --upgrade pip wheel \
 && pip install -e ".[full,fund-workers]"

# ---------------------------------------------------------------------------
# Stage 2: runtime — minimal image, non-root, venv only
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ARG GIT_SHA="unknown"
ARG GIT_TAG="unknown"
ARG BUILD_DATE="unknown"

LABEL org.opencontainers.image.title="coherence-engine" \
      org.opencontainers.image.description="Coherence Fund API + workers" \
      org.opencontainers.image.source="https://github.com/Michael-Quintin/coherence-engine" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${GIT_TAG}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app \
    PORT=8010

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd  --system --uid 10001 --gid app --home "$APP_HOME" --shell /usr/sbin/nologin app \
 && mkdir -p "$APP_HOME" "$APP_HOME/var" \
 && chown -R app:app "$APP_HOME"

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . "$APP_HOME"

WORKDIR $APP_HOME
USER app

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/live" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "server.fund.app:app", "--host", "0.0.0.0", "--port", "8010"]
