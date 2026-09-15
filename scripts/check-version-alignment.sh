#!/bin/sh
# check-version-alignment.sh — mushpi-mock ⇔ mushpi-grow version alignment check
# (replaces the old inline snippet that used to live in AGENTS.md/README.md).
#
# Compares THREE values; all must be identical:
#   1. ../mushpi-grow/app/state.py  _SOFTWARE_VERSION   <- single source of truth
#   2. src/mushpi_mock/VERSION                            (represented, plain text)
#   3. src/mushpi_mock/state.py     _SOFTWARE_VERSION     (represented, runtime constant)
#
# Run it by hand. Anchors all paths to this script's own location, so it behaves
# identically from any CWD. Read-only:
# never mutates any file, git state, or database.
#
# Exit codes: 0 aligned | 1 mismatch / extraction failure | 2 grow checkout not found

set -u

SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd)
MOCK_ROOT=$(CDPATH='' cd "$SCRIPT_DIR/.." && pwd)

GROW_STATE="$MOCK_ROOT/../mushpi-grow/app/state.py"
MOCK_VERSION_FILE="$MOCK_ROOT/src/mushpi_mock/VERSION"
MOCK_STATE="$MOCK_ROOT/src/mushpi_mock/state.py"

PREFIX='version-align:'

fail() {
    echo "$PREFIX $*" >&2
    exit 1
}

[ -f "$GROW_STATE" ] || {
    echo "$PREFIX ERROR: cannot locate grow source of truth: $GROW_STATE" >&2
    echo "$PREFIX        (expected sibling checkout ../mushpi-grow/ — is it present?)" >&2
    exit 2
}
[ -f "$MOCK_VERSION_FILE" ] || fail "missing $MOCK_VERSION_FILE"
[ -f "$MOCK_STATE" ]        || fail "missing $MOCK_STATE"

# Extract the first   _SOFTWARE_VERSION = "x.y.z"   assignment (skips comments/usages).
extract_const() {
    grep -o '_SOFTWARE_VERSION[[:space:]]*=[[:space:]]*"[^"]*"' "$1" \
        | sed -n '1s/.*"\([^"]*\)".*/\1/p'
}

# VERSION file: line 1, all whitespace stripped. NOTE: the p command (not the
# s///p flag) is deliberate — the s flag only prints when a substitution
# happens, and a version line like "0.8.4" has no whitespace to substitute.
read_version_file() {
    sed -n '1{s/[[:space:]]//g;p}' "$1"
}

# Strict MAJOR.MINOR.PATCH shape: digits and dots only, exactly two dots,
# no empty component (rejects '', '1.2', '1.2.3.4', '1..2', '.1.2', '1.2.').
is_semver_shape() {
    case $1 in
        '' | *[!0-9.]* | *.*.*.*) return 1 ;;
        *.*.*)
            case $1 in
                .* | *. | *..*) return 1 ;;
                *) return 0 ;;
            esac ;;
        *) return 1 ;;
    esac
}

grow_ver=$(extract_const "$GROW_STATE")
mock_state_ver=$(extract_const "$MOCK_STATE")
mock_file_ver=$(read_version_file "$MOCK_VERSION_FILE")

[ -n "$grow_ver" ]       || fail "could not extract _SOFTWARE_VERSION from $GROW_STATE (format changed?)"
[ -n "$mock_state_ver" ] || fail "could not extract _SOFTWARE_VERSION from $MOCK_STATE (format changed?)"
[ -n "$mock_file_ver" ]  || fail "could not read version from $MOCK_VERSION_FILE (empty?)"

for v in "$grow_ver" "$mock_state_ver" "$mock_file_ver"; do
    is_semver_shape "$v" || fail "malformed version '$v' (expected MAJOR.MINOR.PATCH)"
done

if [ "$grow_ver" = "$mock_file_ver" ] && [ "$grow_ver" = "$mock_state_ver" ]; then
    echo "$PREFIX OK — grow=$grow_ver  VERSION=$mock_file_ver  state.py=$mock_state_ver"
    exit 0
fi

echo "$PREFIX VERSION MISMATCH" >&2
echo "$PREFIX   grow   ../mushpi-grow/app/state.py  _SOFTWARE_VERSION : $grow_ver   <- source of truth" >&2
echo "$PREFIX   mock   src/mushpi_mock/VERSION                         : $mock_file_ver" >&2
echo "$PREFIX   mock   src/mushpi_mock/state.py     _SOFTWARE_VERSION  : $mock_state_ver" >&2
echo "$PREFIX   Fix: update BOTH mock locations to $grow_ver (never edit grow's value from the mock)." >&2
exit 1
