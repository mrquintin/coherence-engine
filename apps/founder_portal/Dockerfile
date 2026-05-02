# syntax=docker/dockerfile:1.7
# Founder portal — Next.js 14 (App Router) standalone image (prompt 67).
#
# The Next.js standalone output is the smallest production runtime; it
# bundles only the files actually referenced from the request handlers.

# ---------------------------------------------------------------------------
# Stage 1: deps
# ---------------------------------------------------------------------------
FROM node:20-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install --no-audit --no-fund; fi

# ---------------------------------------------------------------------------
# Stage 2: build
# ---------------------------------------------------------------------------
FROM node:20-bookworm-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM node:20-bookworm-slim AS runtime

ARG GIT_SHA="unknown"
ARG GIT_TAG="unknown"
ARG BUILD_DATE="unknown"

LABEL org.opencontainers.image.title="coherence-founder-portal" \
      org.opencontainers.image.description="Coherence Fund founder-facing Next.js portal" \
      org.opencontainers.image.source="https://github.com/Michael-Quintin/coherence-engine" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${GIT_TAG}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3001 \
    HOSTNAME=0.0.0.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd  --system --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Prefer Next.js standalone output if present; fall back to a full copy.
COPY --from=build --chown=app:app /app/public ./public
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static

USER app
EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "server.js"]
