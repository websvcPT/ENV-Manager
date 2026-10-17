#!/usr/bin/env sh
# Make sure the development dataset exists in the running stack. Seeding an already seeded
# database is not an error; any other failure is.
set -u

output=$(docker compose exec -T app envmanager dev seed 2>&1)
status=$?
if [ "$status" -eq 0 ]; then
    echo "seeded the development dataset"
elif echo "$output" | grep -q "already seeded"; then
    echo "development dataset already seeded"
else
    echo "$output" >&2
    exit "$status"
fi
