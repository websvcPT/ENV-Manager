#!/usr/bin/env sh
# Move the development database aside so the app builds a fresh one when it starts again.
# Development only: it refuses unless the running app container is in the development
# environment. The old database and its -wal and -shm files stay next to it as
# envmanager.db*.before-reset, replacing the copy of the previous reset; to go back, stop the app
# and move the files back without the suffix. The master key comes from the compose file, so the
# fresh database is sealed with the same key. `make dev-reset` waits for the app and seeds the
# dataset afterwards.
set -eu

database="dev/data/envmanager.db"

docker compose exec -T app sh -c 'test "$ENVMANAGER_ENVIRONMENT" = development' \
    || { echo "the development stack is not running in the development environment; run make up-bg"; exit 1; }

docker compose stop app

for suffix in "" "-wal" "-shm"; do
    rm -f "$database$suffix.before-reset"
    if [ -e "$database$suffix" ]; then
        mv "$database$suffix" "$database$suffix.before-reset"
    fi
done

docker compose start app
echo "the old database is kept as $database.before-reset"
