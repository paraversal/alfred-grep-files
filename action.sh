#!/bin/bash
# Grep Files: Run Script wrapper. Resolves a usable python3 for Alfred's
# minimal environment, then hands control to action.py.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

for candidate in \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3
do
    if [ -x "$candidate" ]; then
        exec "$candidate" "$HERE/action.py" "$@"
    fi
done

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$HERE/action.py" "$@"
fi

echo "Grep Files: python3 not found" >&2
exit 1
