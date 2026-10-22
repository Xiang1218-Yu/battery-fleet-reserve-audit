#!/bin/sh
set -eu
TARGET_DIR=${TARGET_DIR:-/app}
cp "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/reserve_audit/core.py" "$TARGET_DIR/reserve_audit/core.py"
