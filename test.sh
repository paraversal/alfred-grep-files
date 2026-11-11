#!/bin/bash
# Self-contained smoke test for the Grep Files workflow.
#
# Creates a throwaway tree under /tmp, runs the Script Filter backend against
# it, and checks names, content, exclusions, smart case, actions and the shell
# wrappers. It never touches the real home folder.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d /tmp/agf-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

export alfred_workflow_data="$TMP/data"
export alfred_workflow_cache="$TMP/cache"
export search_root="$TMP/root"
export excludes_mode=builtin

mkdir -p "$TMP/root/src" "$TMP/root/node_modules/pkg" "$TMP/root/.git" "$TMP/bin"
mkdir -p "$TMP/root/Project/Media/Audio Files" "$TMP/root/Music/Song.logicproject"
printf 'alpha beta\nonly alpha\nonly beta\nalpha and beta again\nneedle only here\nAlpha Case\n' > "$TMP/root/src/a.txt"
printf 'needle in dependency\n' > "$TMP/root/node_modules/pkg/dep.txt"
printf 'needle in git\n' > "$TMP/root/.git/config"
printf 'logic audio\n' > "$TMP/root/Project/Media/Audio Files/take.wav"
printf 'logic project\n' > "$TMP/root/Music/Song.logicproject/info.txt"
printf 'keep music\n' > "$TMP/root/Music/keep.txt"
export extra_excludes='Media/Audio Files,.logicproject'

cat > "$TMP/bin/pbcopy" <<'SH'
#!/bin/bash
cat > "$TMP_COPY_TARGET"
SH
chmod +x "$TMP/bin/pbcopy"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# --- workflow icon ---------------------------------------------------------
/usr/bin/python3 - "$ROOT/icon.png" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read(24)
assert data[:8] == b"\x89PNG\r\n\x1a\n", "icon.png is not a PNG"
width, height = struct.unpack(">II", data[16:24])
assert (width, height) == (512, 512), (width, height)
PY

# --- Configuration Builder contract ----------------------------------------
/usr/bin/python3 - "$ROOT" <<'PY'
import os
import plistlib
import sys

root = sys.argv[1]
with open(root + "/info.plist", "rb") as handle:
    data = plistlib.load(handle)

assert os.path.isfile(root + "/CONFIGURATION.md"), "CONFIGURATION.md is missing"
readme = data.get("readme", "")
assert "Configure Workflow" in readme, "embedded readme does not point at Configure Workflow"
assert "CONFIGURATION.md" in readme, "embedded readme does not point at CONFIGURATION.md"

config = {entry["variable"]: entry for entry in data.get("userconfigurationconfig", [])}
expected = {
    "grep_keyword",
    "search_root",
    "excludes_mode",
    "asimeow_config",
    "extra_excludes",
    "content_enabled",
    "max_items",
    "max_content_lines",
    "include_dirs",
    "editor_command",
    "index_ttl_seconds",
}
missing = expected - set(config)
assert not missing, "missing Configuration Builder fields: %s" % sorted(missing)
for variable, entry in config.items():
    assert entry.get("label", "").strip(), "missing label for %s" % variable
    assert entry.get("description", "").strip(), "missing description for %s" % variable
assert config["grep_keyword"]["config"]["default"] == "f"
assert config["search_root"]["config"]["default"] == "~"
assert config["excludes_mode"]["config"]["default"] == "asimeow"
assert config["excludes_mode"]["config"]["pairs"] == [
    ["asimeow config", "asimeow"],
    ["Built-in list", "builtin"],
    ["No exclusions", "none"],
]

filters = [o for o in data["objects"] if o.get("type") == "alfred.workflow.input.scriptfilter"]
assert len(filters) == 1, filters
assert filters[0]["config"]["keyword"] == "{var:grep_keyword}", filters[0]["config"]["keyword"]
assert set(data.get("variables", {})) == {"PATH"}, data.get("variables")
PY

# --- index -----------------------------------------------------------------
/usr/bin/python3 "$ROOT/search.py" --rebuild >/dev/null
if ! grep -qx 'src/a.txt' "$TMP/data/paths.txt"; then
    fail "index missing src/a.txt"
fi
if grep -q 'node_modules' "$TMP/data/paths.txt"; then
    fail "index leaked node_modules"
fi
if grep -q '\.git/' "$TMP/data/paths.txt"; then
    fail "index leaked .git"
fi
if grep -q 'Media/Audio Files' "$TMP/data/paths.txt"; then
    fail "index leaked path-pattern exclusion Media/Audio Files"
fi
if grep -q 'logicproject' "$TMP/data/paths.txt"; then
    fail "index leaked suffix exclusion .logicproject"
fi
if ! grep -qx 'Music/keep.txt' "$TMP/data/paths.txt"; then
    fail "path-pattern exclusion was too broad"
fi

names_json() {
    local query="$1"
    local out="$2"
    /usr/bin/python3 "$ROOT/search.py" "$query" > "$out"
}

wait_for_content() {
    local query="$1"
    local out="$2"
    local attempt
    for attempt in $(seq 1 40); do
        names_json "$query" "$out"
        if /usr/bin/python3 - "$out" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
raise SystemExit(0 if any(i.get("variables", {}).get("line") not in (None, "", "0") for i in items) else 1)
PY
        then
            return 0
        fi
        sleep 0.25
    done
    fail "content results never arrived for “$query”"
}

# --- name search -----------------------------------------------------------
names_json "a" "$TMP/qname.json"
/usr/bin/python3 - "$TMP/qname.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
titles = [i["title"] for i in items]
assert "📄 a.txt" in titles, titles
assert all("node_modules" not in i.get("arg", "") for i in items)
assert all(".git/" not in i.get("arg", "") for i in items)
PY

# --- folder emoji ----------------------------------------------------------
names_json "src" "$TMP/qdir.json"
/usr/bin/python3 - "$TMP/qdir.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
assert any(i["title"] == "📁 src/" for i in items), [i["title"] for i in items]
PY

# --- content search (deferred second pass) ---------------------------------
wait_for_content "needle" "$TMP/qcontent.json"
/usr/bin/python3 - "$TMP/qcontent.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
content = [i for i in items if i.get("variables", {}).get("line") not in (None, "", "0")]
assert content, items
assert all("node_modules" not in i.get("arg", "") for i in content)
assert all(".git/" not in i.get("arg", "") for i in content)
assert all(i["title"].startswith("🔎 ") for i in content), content
assert any("needle" in i["title"].lower() for i in content), content
PY

# --- multi-token means same line ------------------------------------------
wait_for_content "alpha beta" "$TMP/qand.json"
/usr/bin/python3 - "$TMP/qand.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
content = [i for i in items if i.get("variables", {}).get("line") not in (None, "", "0")]
assert len(content) == 2, [(i["title"], i["subtitle"]) for i in content]
PY

# --- smart case ------------------------------------------------------------
wait_for_content "Alpha" "$TMP/qcase.json"
/usr/bin/python3 - "$TMP/qcase.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
content = [i for i in items if i.get("variables", {}).get("line") not in (None, "", "0")]
assert len(content) == 1, [(i["title"], i["subtitle"]) for i in content]
assert content[0]["title"].startswith("🔎 "), content[0]
assert "Alpha Case" in content[0]["title"], content[0]
PY

# --- empty query hint -------------------------------------------------------
/usr/bin/python3 "$ROOT/search.py" "" > "$TMP/qempty.json"
/usr/bin/python3 - "$TMP/qempty.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
assert len(items) >= 3, items
assert items[0]["title"].startswith("Search "), items[0]
PY

# --- shell wrapper ----------------------------------------------------------
./search.sh "a" > "$TMP/qwrapper.json"
/usr/bin/python3 - "$TMP/qwrapper.json" <<'PY'
import json, sys
json.load(open(sys.argv[1]))
PY

# --- action dispatcher ------------------------------------------------------
export TMP_COPY_TARGET="$TMP/copied.txt"
export PATH="$TMP/bin:$PATH"
(
    export action=copy line=3 col=5
    "$ROOT/action.sh" "$TMP/root/src/a.txt"
)
test "$(cat "$TMP/copied.txt")" = "$TMP/root/src/a.txt:3" || fail "copy action produced $(cat "$TMP/copied.txt")"

# --- content scans must not leak rg when superseded ------------------------
# A slow stub rg proves the scan supervisor and rg share a process group: a
# new query kills the old supervisor, which must take rg down with it.
cat > "$TMP/bin/rg" <<'SH'
#!/bin/bash
printf '%s\n' "$$" >> "$AGF_TEST_RG_LOG"
sleep 30
SH
chmod +x "$TMP/bin/rg"
export AGF_TEST_RG_LOG="$TMP/rg.pids"
: > "$AGF_TEST_RG_LOG"

names_json "leakone" "$TMP/qleak1.json"
for _ in $(seq 1 100); do
    if [ -s "$AGF_TEST_RG_LOG" ]; then
        break
    fi
    sleep 0.05
done
first_rg="$(head -n 1 "$AGF_TEST_RG_LOG" 2>/dev/null || true)"
if [ -z "$first_rg" ]; then
    fail "stub rg never started for the first content scan"
fi

names_json "leaktwo" "$TMP/qleak2.json"
for _ in $(seq 1 40); do
    if ! kill -0 "$first_rg" 2>/dev/null; then
        break
    fi
    sleep 0.05
done
if kill -0 "$first_rg" 2>/dev/null; then
    fail "superseded content scan left rg $first_rg running"
fi

# Stop the second scan (its stub rg would otherwise idle for 30 s).
for _ in $(seq 1 40); do
    if [ -s "$TMP/cache/content/scan.pid" ]; then
        break
    fi
    sleep 0.05
done
if [ -s "$TMP/cache/content/scan.pid" ]; then
    scan_pid="$(cut -d' ' -f1 "$TMP/cache/content/scan.pid")"
    kill -9 "-$scan_pid" 2>/dev/null || true
fi
while read -r pid; do
    kill -9 "$pid" 2>/dev/null || true
done < "$AGF_TEST_RG_LOG"

echo "OK: docs, configuration, index, path exclusions, names, deferred content, AND, smart case, hint, wrapper, action, scan cleanup"
