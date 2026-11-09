#!/bin/sh
# check-version-alignment.sh — mushpi-mock ⇔ mushpi-grow version alignment check
# (replaces the old inline snippet that used to live in AGENTS.md/README.md).
#
# Runs three checks:
#
# SIMULATED FIRMWARE version — compares TWO values; both must be identical:
#   1. ../mushpi-grow/app/state.py  _SOFTWARE_VERSION   <- single source of truth
#   2. src/mushpi_mock/state.py     _SOFTWARE_VERSION     (what the mock simulates)
#   The mock impersonates a Pico, so the firmware version it announces and
#   serves must be the version a real unit would report. This is the invariant
#   the check exists for.
#
# MOCK'S OWN version — compares TWO values; both must be identical:
#   1. src/mushpi_mock/VERSION                             (release version,
#      guarded against the image tag by the publish workflow)
#   2. pyproject.toml  version                             (package metadata)
#   The mock's own version MAY LEAD the firmware version it simulates — this
#   divergence is deliberate and permanent: the mock image releases on its own
#   tag as a tool, while the simulated firmware version tracks the real
#   firmware. Never "fix" a divergence by moving one identity onto the other.
#
# API version — compares TWO values (REST contract generation):
#   1. ../mushpi-grow/app/state.py  _API_VERSION        <- single source of truth
#   2. src/mushpi_mock/state.py     _API_VERSION        (represented, runtime constant)
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
MOCK_PYPROJECT="$MOCK_ROOT/pyproject.toml"
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
[ -f "$MOCK_PYPROJECT" ]     || fail "missing $MOCK_PYPROJECT"
[ -f "$MOCK_STATE" ]        || fail "missing $MOCK_STATE"

# Extract the first   _SOFTWARE_VERSION = "x.y.z"   assignment (skips comments/usages).
extract_const() {
    grep -o '_SOFTWARE_VERSION[[:space:]]*=[[:space:]]*"[^"]*"' "$1" \
        | sed -n '1s/.*"\([^"]*\)".*/\1/p'
}

# Extract the first   _API_VERSION = N   assignment (integer, no quotes).
# Comments referencing _API_VERSION never match because they lack the '=';
# usages like "api_version": _API_VERSION, don't match either (',' not digit).
extract_api_const() {
    grep -o '_API_VERSION[[:space:]]*=[[:space:]]*[0-9][0-9]*' "$1" \
        | sed -n '1s/.*=[[:space:]]*//p'
}

# VERSION file: line 1, all whitespace stripped. NOTE: the p command (not the
# s///p flag) is deliberate — the s flag only prints when a substitution
# happens, and a version line like "0.8.4" has no whitespace to substitute.
read_version_file() {
    sed -n '1{s/[[:space:]]//g;p}' "$1"
}

# Extract the project-table   version = "x.y.z"   from pyproject.toml. Anchored
# at column 0 so only the top-level (project) key matches, first occurrence wins.
extract_pyproject_version() {
    grep -o '^version[[:space:]]*=[[:space:]]*"[^"]*"' "$1" \
        | sed -n '1s/.*"\([^"]*\)".*/\1/p'
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
mock_pyproject_ver=$(extract_pyproject_version "$MOCK_PYPROJECT")
grow_api=$(extract_api_const "$GROW_STATE")
mock_api=$(extract_api_const "$MOCK_STATE")

[ -n "$grow_ver" ]         || fail "could not extract _SOFTWARE_VERSION from $GROW_STATE (format changed?)"
[ -n "$mock_state_ver" ]   || fail "could not extract _SOFTWARE_VERSION from $MOCK_STATE (format changed?)"
[ -n "$mock_file_ver" ]    || fail "could not read version from $MOCK_VERSION_FILE (empty?)"
[ -n "$mock_pyproject_ver" ] || fail "could not extract project version from $MOCK_PYPROJECT (format changed?)"
[ -n "$grow_api" ]         || fail "could not extract _API_VERSION from $GROW_STATE (missing? format changed?)"
[ -n "$mock_api" ]         || fail "could not extract _API_VERSION from $MOCK_STATE (missing? format changed?)"

for v in "$grow_ver" "$mock_state_ver" "$mock_file_ver" "$mock_pyproject_ver"; do
    is_semver_shape "$v" || fail "malformed version '$v' (expected MAJOR.MINOR.PATCH)"
done

# All three checks run to completion (pass/fail line each); exit 1 if any failed.
rc=0

# ── Check 1: simulated firmware version (mock state.py ⇔ grow) ────────────
if [ "$grow_ver" = "$mock_state_ver" ]; then
    echo "$PREFIX FIRMWARE OK — grow=$grow_ver  mock state.py=$mock_state_ver"
else
    rc=1
    echo "$PREFIX SIMULATED FIRMWARE VERSION MISMATCH" >&2
    echo "$PREFIX   grow   ../mushpi-grow/app/state.py  _SOFTWARE_VERSION  : $grow_ver   <- source of truth" >&2
    echo "$PREFIX   mock   src/mushpi_mock/state.py     _SOFTWARE_VERSION  : $mock_state_ver" >&2
    echo "$PREFIX   Fix: update the mock constant to $grow_ver (never edit grow's value from the mock)." >&2
    echo "$PREFIX        This is what the mock announces as firmware_version. The mock's OWN release version (check 2) is a separate identity and may lead this one deliberately — do not drag it back here." >&2
fi

# ── Check 2: mock's own release version (VERSION ⇔ pyproject.toml) ────────
if [ "$mock_file_ver" = "$mock_pyproject_ver" ]; then
    echo "$PREFIX MOCK VERSION OK — VERSION=$mock_file_ver  pyproject.toml=$mock_pyproject_ver"
else
    rc=1
    echo "$PREFIX MOCK VERSION MISMATCH" >&2
    echo "$PREFIX   mock   src/mushpi_mock/VERSION : $mock_file_ver   <- guarded against the image tag by publish.yml" >&2
    echo "$PREFIX   mock   pyproject.toml version  : $mock_pyproject_ver" >&2
    echo "$PREFIX   Fix: bring both mock-owned version fields to the same value and regenerate uv.lock." >&2
    echo "$PREFIX        Deliberately NOT compared to the firmware version above — the mock's own version may lead what it simulates." >&2
fi

# ── Check 3: API contract version (two-way) ────────────────────────────────
if [ "$grow_api" = "$mock_api" ]; then
    echo "$PREFIX API OK — grow _API_VERSION=$grow_api  mock _API_VERSION=$mock_api"
else
    rc=1
    echo "$PREFIX API VERSION MISMATCH" >&2
    echo "$PREFIX   grow   ../mushpi-grow/app/state.py  _API_VERSION : $grow_api   <- source of truth" >&2
    echo "$PREFIX   mock   src/mushpi_mock/state.py     _API_VERSION : $mock_api" >&2
    echo "$PREFIX   Fix: update the mock constant to $grow_api (never edit grow's value from the mock)." >&2
fi

exit $rc
