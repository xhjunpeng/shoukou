#!/usr/bin/env bash
# Read-only inventory and cleanup preflight. Never removes files or Git refs.
set -eu
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/inspect_repo.py" "$@"
