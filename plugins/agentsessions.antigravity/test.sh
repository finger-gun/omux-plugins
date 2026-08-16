#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd "$(dirname "$0")" && pwd)"
temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT
db_path="$temp_dir/state.vscdb"

DB_PATH="$db_path" python3 - <<'PY'
import base64
import os
import sqlite3


def varint(value):
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def field(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def text_field(number, value):
    return field(number, value.encode("utf-8"))


summary = (
    text_field(1, "Fix terminal resize handling for ISO keyboard layouts")
    + field(4, text_field(1, "file:///Users/example/openmux%20project"))
)
envelope = text_field(1, base64.b64encode(summary).decode("ascii"))
valid_record = (
    text_field(1, "123e4567-e89b-12d3-a456-426614174000")
    + field(2, envelope)
)
invalid_record = text_field(1, "not-a-valid-session") + field(2, text_field(1, "not-base64"))
trajectories = field(1, valid_record) + field(1, invalid_record)

connection = sqlite3.connect(os.environ["DB_PATH"])
connection.execute("create table ItemTable (key text unique, value blob)")
connection.execute(
    "insert into ItemTable(key, value) values (?, ?)",
    ("antigravityUnifiedStateSync.trajectorySummaries", base64.b64encode(trajectories)),
)
connection.commit()
PY

output="$(ANTIGRAVITY_STATE_DB="$db_path" "$plugin_dir/plugin" __omux_agent_sessions discover)"
OUTPUT="$output" DB_PATH="$db_path" python3 - <<'PY'
import json
import os

rows = json.loads(os.environ["OUTPUT"])
assert rows == [{
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "title": "Fix terminal resize handling for ISO keyboard layouts",
    "cwd": "/Users/example/openmux project",
    "source_path": os.environ["DB_PATH"],
}]
PY

missing_output="$(ANTIGRAVITY_STATE_DB="$temp_dir/missing.vscdb" "$plugin_dir/plugin" discover)"
[[ "$missing_output" == "[]" ]]

empty_db_path="$temp_dir/empty.vscdb"
EMPTY_DB_PATH="$empty_db_path" python3 - <<'PY'
import os
import sqlite3

connection = sqlite3.connect(os.environ["EMPTY_DB_PATH"])
connection.execute("create table ItemTable (key text unique, value blob)")
connection.commit()
PY
empty_output="$(ANTIGRAVITY_STATE_DB="$empty_db_path" "$plugin_dir/plugin" discover)"
[[ "$empty_output" == "[]" ]]

if ANTIGRAVITY_STATE_DB="$db_path" "$plugin_dir/plugin" __omux_agent_sessions unsupported >/dev/null 2>&1; then
  echo "unsupported callback action unexpectedly succeeded" >&2
  exit 1
fi
