#!/bin/sh
# The quarterly drill. Exits non-zero when the restored database fails a gate, so a
# scheduled run nobody is watching still leaves a record of having failed.
set -eu
cd "$(dirname "$0")/.."
exec python scripts/backup.py verify
