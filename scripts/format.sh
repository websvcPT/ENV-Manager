#!/usr/bin/env sh
set -e
set -x

ruff check app --fix
ruff format app
