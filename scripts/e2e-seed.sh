#!/usr/bin/env sh
# Make sure the development dataset exists in the running stack, then clear the lockout state
# that earlier browser runs left behind.
set -eu

sh "$(dirname "$0")/seed-dev.sh"

# Failed logins from earlier runs would count against the lockout limits of this one.
docker compose exec -T app envmanager dev reset-lockouts
