#!/bin/bash
# Build the installable Alfred workflow archive.
#
# The archive root must contain info.plist; the two shell entry points must be
# executable because Alfred launches the external script files directly.

set -euo pipefail

cd "$(dirname "$0")"

OUT="alfred-grep-files.alfredworkflow"
FILES=(
    info.plist
    search.py
    search.sh
    action.py
    action.sh
    README.md
    CONFIGURATION.md
)
if [[ -f icon.png ]]; then
    FILES+=(icon.png)
fi

chmod 644 info.plist search.py action.py README.md CONFIGURATION.md
chmod 755 search.sh action.sh

rm -f "$OUT"
zip -q -X "$OUT" "${FILES[@]}"
echo "Built $OUT"
unzip -l "$OUT"
