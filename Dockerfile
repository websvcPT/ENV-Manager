# The Tailwind standalone CLI is a glibc binary, so this stage is Debian.
FROM debian:bookworm-slim AS assets
ARG TAILWIND_VERSION=4.3.3
ARG TARGETARCH=amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && apt-get clean \
    && find /var/lib/apt/lists -type f -delete
RUN case "$TARGETARCH" in \
        amd64) TAILWIND_ARCH=x64 ;; \
        arm64) TAILWIND_ARCH=arm64 ;; \
        *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-${TAILWIND_ARCH}" \
    && chmod +x /usr/local/bin/tailwindcss
WORKDIR /build
COPY app/assets app/assets
COPY app/templates app/templates
COPY app/static/app.js app/static/app.js
RUN tailwindcss -i app/assets/app.css -o app/static/style.css --minify

FROM python:3.14-slim AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

FROM python-base AS deps
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python-base AS deps-dev
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

FROM python:3.14-slim AS runtime
ARG APP_UID=1000
ARG APP_GID=1000
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/opt/venv/bin:$PATH \
    ENVMANAGER_DATA_PATH=/data
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && apt-get clean \
    && find /var/lib/apt/lists -type f -delete \
    && groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /data \
    && chown app:app /data
COPY --from=docker:29.9.1-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29.9.1-cli /usr/local/libexec/docker/cli-plugins/docker-compose \
    /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY start.sh alembic.ini ./
COPY --from=assets /build/app/static/style.css ./app/static/style.css
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ENVMANAGER_PORT','8080'),timeout=4)"]
CMD ["sh", "start.sh"]

FROM runtime AS test
USER root
COPY --from=deps-dev /opt/venv /opt/venv
COPY pyproject.toml ./
COPY scripts ./scripts
USER app
