#!/bin/bash
# Grep Files: Script Filter wrapper.
#
# Alfred starts script filters with a minimal PATH, which usually does not
# include /opt/homebrew/bin. Resolve python3 explicitly, then exec so Alfred
# can kill the whole process group on the next keystroke.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

for candidate in \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3
do
    if [ -x "$candidate" ]; then
        exec "$candidate" "$HERE/search.py" "$@"
    fi
done

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$HERE/search.py" "$@"
fi

printf '%s' '{"items":[{"title":"python3 not found","subtitle":"Install the Xcode command line tools or Homebrew python3","valid":false}]}'
exit 0
