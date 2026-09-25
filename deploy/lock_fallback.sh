#!/bin/sh
# Lock the Phase 5 DuckDB audit fallback after the traffic switch.
# This is intentionally a separate operator action: the application cannot
# safely serve writes from a read-only DuckDB file.
set -eu

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "usage: $0 /path/to/hrms.duckdb" >&2
    exit 2
fi

chmod a-w "$1"
printf 'locked read-only: %s (%s)\n' "$1" "$(stat -c '%A' "$1" 2>/dev/null || stat -f '%Sp' "$1")"
