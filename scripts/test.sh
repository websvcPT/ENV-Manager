#!/usr/bin/env sh
set -e
set -x

export ENVMANAGER_ENVIRONMENT="${ENVMANAGER_ENVIRONMENT:-testing}"
export ENVMANAGER_CSRF_SECRET="${ENVMANAGER_CSRF_SECRET:-test-not-secret-0123456789abcdef0123456789}"

exec pytest -p no:cacheprovider "$@"
