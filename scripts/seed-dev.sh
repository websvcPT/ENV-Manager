#!/usr/bin/env sh
# Make sure the development dataset, including the demo project, exists in the running stack.
# Seeding an already seeded database is not an error; any other failure is. The demo project is
# rooted at DEMO_ROOT (the demo folder of this checkout by default); only the database rows
# depend on it, so it works whether or not the stack mounts that folder.
set -u

demo_root="${DEMO_ROOT:-$(pwd)/demo}"
output=$(docker compose exec -T -e ENVMANAGER_DEMO_ROOT="$demo_root" app envmanager dev seed 2>&1)
status=$?
if [ "$status" -eq 0 ]; then
    echo "seeded the development dataset"
elif echo "$output" | grep -q "already seeded"; then
    echo "development dataset already seeded, demo project in place"
else
    echo "$output" >&2
    exit "$status"
fi
