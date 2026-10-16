#!/bin/sh
# check-unit-page-drift.sh — mushpi-mock ⇔ mushpi-grow unit-page drift check.
#
# "Checked vendored copy": the mock serves byte-identical copies of the firmware
# page assets (unit.html, unit.js, favicon.svg). The mock-only differences
# (simulated labels + restart caveat) live in unit_page.py at render time, NOT
# in these bytes — so an exact hash comparison is the right staleness guard,
# in the style of check-version-alignment.sh.
#
# For each asset it answers two questions:
#   DRIFTED   grow's current file != the mock's vendored copy
#             → run scripts/refresh-unit-page.sh, then re-check unit_page.py anchors
#   TAMPERED  the mock's vendored copy != the hash recorded in static/VENDOR
#             → the vendor was hand-edited; re-copy or re-generate VENDOR
#
# Anchors all paths to this script's own location, so it behaves identically from
# any CWD. Read-only: never mutates any file, git state, or database.
#
# Exit codes: 0 all aligned | 1 drift / tamper / extraction failure | 2 grow or VENDOR not found

set -u

SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd)
MOCK_ROOT=$(CDPATH='' cd "$SCRIPT_DIR/.." && pwd)

GROW_ROOT="$MOCK_ROOT/../mushpi-grow"
STATIC_DIR="$MOCK_ROOT/src/mushpi_mock/static"
VENDOR="$STATIC_DIR/VENDOR"

PREFIX='unit-page-drift:'

fail() {
    echo "$PREFIX $*" >&2
}

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
    fi
}

[ -d "$GROW_ROOT/app/static" ] || {
    echo "$PREFIX ERROR: cannot locate grow page assets: $GROW_ROOT/app (is the checkout present?)" >&2
    exit 2
}
[ -f "$VENDOR" ] || {
    echo "$PREFIX ERROR: no provenance manifest at $VENDOR (run scripts/refresh-unit-page.sh first)" >&2
    exit 2
}

# asset name -> grow source path (closed set)
asset_source() {
    case $1 in
        unit.html)   echo "app/unit.html" ;;
        unit.js)     echo "app/static/unit.js" ;;
        favicon.svg) echo "app/static/favicon.svg" ;;
        *)           echo "" ;;
    esac
}

# Recorded hash for an asset line:  asset <name> <sha> source <path>
recorded_hash() {
    awk -v n="$1" '$1=="asset" && $2==n {print $3; exit}' "$VENDOR"
}

rc=0
checked=0
while read -r kind name recorded rest; do
    [ "$kind" = "asset" ] || continue
    checked=$((checked + 1))
    mock="$STATIC_DIR/$name"
    grow="$GROW_ROOT/$(asset_source "$name")"

    [ -f "$mock" ] || { fail "MISSING  $name — not vendored (src/mushpi_mock/static/$name)"; rc=1; continue; }
    [ -f "$grow" ] || { fail "MISSING  $name — grow source $grow gone"; rc=1; continue; }
    [ -n "$recorded" ] || { fail "NO RECORD for $name in $VENDOR"; rc=1; continue; }

    mock_hash=$(hash_file "$mock")
    grow_hash=$(hash_file "$grow")

    if [ "$mock_hash" != "$recorded" ]; then
        rc=1
        echo "$PREFIX TAMPERED $name — vendored copy != recorded in VENDOR" >&2
        echo "$PREFIX   vendored : $mock_hash" >&2
        echo "$PREFIX   recorded : $recorded" >&2
    fi
    if [ "$grow_hash" != "$mock_hash" ]; then
        rc=1
        echo "$PREFIX DRIFTED  $name — the firmware page moved" >&2
        echo "$PREFIX   grow     : $grow_hash   <- source of truth" >&2
        echo "$PREFIX   vendored : $mock_hash" >&2
    fi
    if [ "$grow_hash" = "$mock_hash" ] && [ "$mock_hash" = "$recorded" ]; then
        echo "$PREFIX OK $name — $mock_hash"
    fi
done <<EOF
$(grep '^asset ' "$VENDOR")
EOF

if [ "$checked" -eq 0 ]; then
    echo "$PREFIX ERROR: no 'asset' lines in $VENDOR (format changed?)" >&2
    exit 1
fi

if [ "$rc" -eq 0 ]; then
    echo "$PREFIX all $checked page assets match grow ($(git -C "$GROW_ROOT" rev-parse --short HEAD 2>/dev/null || echo '?'))"
else
    echo "$PREFIX fix: run scripts/refresh-unit-page.sh, then re-check the MOCK_PAGE_EDITS anchors in src/mushpi_mock/unit_page.py" >&2
fi

exit "$rc"
