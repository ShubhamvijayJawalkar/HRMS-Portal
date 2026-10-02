#!/bin/sh
# Thin wrapper so cron has one stable command to call. `set -e` matters: without it
# a failed backup still exits 0 and cron reports success.
set -eu
cd "$(dirname "$0")/.."
exec python scripts/backup.py backup
