#!/usr/bin/env sh
set -e

HOST=${ENVMANAGER_HOST:-0.0.0.0}
PORT=${ENVMANAGER_PORT:-8080}
LOG_LEVEL=$(echo "${ENVMANAGER_LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')

# Single worker by design: the scheduler and deploy locks live in process.
exec uvicorn ${UVICORN_OPTS:-} --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL" app.main:app
