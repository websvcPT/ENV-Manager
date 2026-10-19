#!/usr/bin/env sh
# Make sure the development dataset exists in the running stack, clear the lockout state that
# earlier browser runs left behind, and remove their old scratch users and projects.
set -eu

sh "$(dirname "$0")/seed-dev.sh"

# Failed logins from earlier runs would count against the lockout limits of this one.
docker compose exec -T app envmanager dev reset-lockouts

# Users cannot be deleted in the application, so every run leaves scratch users behind and the
# users list grows until the specs slow down. Only rows older than an hour are removed, so a run
# that is still going is never touched.
docker compose exec -T app envmanager dev cleanup
