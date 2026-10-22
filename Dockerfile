# syntax=docker/dockerfile:1
# Node LTS for the Remotion renderer (render/). Only the node binary + bundled npm are
# copied into the Python image below.
FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS node

FROM node AS render-deps
WORKDIR /app/render
COPY render/package.json render/package-lock.json ./
# Runtime deps only (no typescript/vitest), then fetch Remotion's pinned
# chrome-headless-shell into node_modules/.remotion at build time (the container's root
# filesystem is read-only at runtime, and renders must not hit the network).
# --ignore-scripts: no dependency lifecycle scripts at build time (only esbuild has one, a
# binary self-check; its platform binary comes from the optional dependency anyway).
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && npx --no-install remotion browser ensure \
    && npm cache clean --force

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

COPY --from=ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 /uv /usr/local/bin/uv

# ffmpeg/tini for the pipeline; the lib* packages are the shared libraries Chrome
# headless shell needs on Debian (https://www.remotion.dev/docs/miscellaneous/linux-dependencies).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates tini fonts-dejavu-core fonts-noto-color-emoji \
        libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libgbm1 libasound2 \
        libxrandr2 libxkbcommon0 libxfixes3 libxcomposite1 libxdamage1 \
        libpango-1.0-0 libcairo2 libcups2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CLIPPER_RENDER_DIR=/app/render

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY --from=render-deps /app/render/node_modules ./render/node_modules
COPY render/package.json render/package-lock.json render/remotion.config.ts render/tsconfig.json ./render/
COPY render/schema ./render/schema
COPY render/src ./render/src

COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 10001 clipper \
    && mkdir -p /app/data /app/output /app/config \
    && chown -R clipper:clipper /app/data /app/output
USER clipper

ENTRYPOINT ["tini", "--", "clipper"]
CMD ["--help"]
