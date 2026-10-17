#!/usr/bin/env sh
set -e

HOST=${ENVMANAGER_HOST:-0.0.0.0}
PORT=${ENVMANAGER_PORT:-8080}
LOG_LEVEL=$(echo "${ENVMANAGER_LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')

# Single worker by design: the scheduler and deploy locks live in process. Forwarded headers are
# handled by the application from ENVMANAGER_TRUSTED_PROXIES, so uvicorn's own handling is off.
exec uvicorn ${UVICORN_OPTS:-} --no-proxy-headers --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL" app.main:app
