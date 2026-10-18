#!/usr/bin/env sh
# Restore the demo target files (the real files next to the dist templates in
# demo/stack/environments) to the known baseline kept in demo/baseline/environments:
# .env.api drifted, .env.db and .env.db.secrets identical to their managed versions,
# demo-ca.pem missing. Files are written with mode 0600 like a deploy writes them.
#
# With --if-missing nothing is touched when a target file exists already; `make demo-up` uses
# that to prepare a fresh checkout without discarding what a demo run has written.
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)/demo"
targets="$root/stack/environments"
baseline="$root/baseline/environments"

mkdir -p "$targets"
if [ "${1:-}" = "--if-missing" ] \
    && [ -n "$(find "$targets" -maxdepth 1 -type f ! -name '*-dist' -print -quit)" ]; then
    exit 0
fi

find "$targets" -maxdepth 1 -type f ! -name '*-dist' -delete
find "$baseline" -maxdepth 1 -type f | while read -r file; do
    install -m 0600 "$file" "$targets/$(basename "$file")"
done
echo "demo target files restored to the baseline"
