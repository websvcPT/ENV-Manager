#!/usr/bin/env sh
# Run the test suite in parallel (pytest-xdist), at most 4 workers: more of them only compete for
# the same cores. A later option wins, so `sh scripts/test.sh -n 0 app/tests/test_login.py` runs
# one file in one process (debugging) and `--maxprocesses 8` raises the cap.
set -e
set -x

export ENVMANAGER_ENVIRONMENT="${ENVMANAGER_ENVIRONMENT:-testing}"
export ENVMANAGER_CSRF_SECRET="${ENVMANAGER_CSRF_SECRET:-test-not-secret-0123456789abcdef0123456789}"

exec pytest -p no:cacheprovider -n auto --maxprocesses 4 "$@"
