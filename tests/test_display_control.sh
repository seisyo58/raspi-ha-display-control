#!/bin/sh

set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
state_file=$(mktemp)
lock_file=$(mktemp)
trap 'rm -f "$state_file" "$lock_file"' EXIT HUP INT TERM

export DISPLAY_CONTROL_WLR_RANDR="$root_dir/tests/fixtures/fake-wlr-randr"
export DISPLAY_CONTROL_LOCK_FILE="$lock_file"
export DISPLAY_CONTROL_RETRIES=2
export DISPLAY_CONTROL_RETRY_INTERVAL=0
export FAKE_WLR_STATE_FILE="$state_file"
export PATH="$root_dir/tests/fixtures:$PATH"
export DISPLAY_OUTPUT=HDMI-A-1

assert_state() {
    expected=$1
    actual=$($root_dir/bin/display-control status)
    [ "$actual" = "$expected" ] || {
        printf 'expected %s, got %s\n' "$expected" "$actual" >&2
        exit 1
    }
}

assert_state OFF
$root_dir/bin/display-control on
assert_state ON
$root_dir/bin/display-control off
assert_state OFF

if $root_dir/bin/display-control invalid >/dev/null 2>&1; then
    printf 'invalid argument unexpectedly succeeded\n' >&2
    exit 1
fi

printf 'ON\n' > "$state_file"
export DISPLAY_OUTPUT=auto
export DISPLAY_OUTPUT_CACHE_FILE="$state_file.cache"
rm -f "$DISPLAY_OUTPUT_CACHE_FILE"
assert_state ON
[ "$(cat "$DISPLAY_OUTPUT_CACHE_FILE")" = "HDMI-A-1" ]
printf 'DISABLED\n' > "$state_file"
assert_state OFF
$root_dir/bin/display-control off
assert_state OFF
$root_dir/bin/display-control on
assert_state ON
