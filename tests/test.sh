#!/bin/sh
set -eu
umask 077

TESTS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$TESTS_DIR" = "/tests" ]; then
    BASE_DIR=/logs/verifier
    REWARD_FILE=${REWARD_FILE:-$BASE_DIR/reward.txt}
    PROGRAM_DIR=${PROGRAM_DIR:-/app}
else
    BASE_DIR=$(CDPATH= cd -- "$TESTS_DIR/.." && pwd)
    REWARD_FILE=${REWARD_FILE:-$BASE_DIR/reward.txt}
    PROGRAM_DIR=${PROGRAM_DIR:-$BASE_DIR/environment}
fi
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3)}
export PROGRAM_DIR

mkdir -p "$BASE_DIR" "$(dirname -- "$REWARD_FILE")"

write_zero_reward() {
    "$PYTHON_BIN" -I - "$REWARD_FILE" <<'PYZERO'
import os
import sys
import tempfile

path = sys.argv[1]
directory = os.path.dirname(os.path.abspath(path)) or "."
fd, temporary = tempfile.mkstemp(prefix=".reward-", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write("0.000000\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except OSError:
        pass
    raise
PYZERO
}

write_zero_reward

TMP=$(mktemp -d "${TMPDIR:-/tmp}/bfra-verifier.XXXXXX")
FINALIZED=0
cleanup() {
    rm -rf "$TMP"
}
on_exit() {
    if [ "$FINALIZED" -ne 1 ]; then
        write_zero_reward || true
    fi
    cleanup
}
trap 'on_exit' EXIT
trap 'FINALIZED=0; exit 130' HUP INT TERM

PROBE_DIR="$TMP/startup-probe"
PROBE_MARKER="$PROBE_DIR/imported"
mkdir -p "$PROBE_DIR" "$TMP/home"
cat > "$PROBE_DIR/sitecustomize.py" <<'PYPROBE'
import atexit
import os
from pathlib import Path

Path(os.environ["PROBE_MARKER"]).write_text("imported", encoding="utf-8")
atexit.register(
    lambda: Path(os.environ["REWARD_FILE"]).write_text(
        "1.000000\n", encoding="utf-8"
    )
)
PYPROBE
if ! (
    cd "$PROBE_DIR" &&
    env -i \
        PATH=/usr/local/bin:/usr/bin:/bin \
        HOME="$TMP/home" \
        TMPDIR=/tmp \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        PYTHONPATH="$PROBE_DIR" \
        PROBE_MARKER="$PROBE_MARKER" \
        REWARD_FILE="$REWARD_FILE" \
        "$PYTHON_BIN" -I -c 'import sys; assert sys.flags.isolated == 1'
)
then
    FINALIZED=1
    write_zero_reward
    exit 0
fi
if [ -e "$PROBE_MARKER" ] || [ "$(cat "$REWARD_FILE" 2>/dev/null || true)" != '0.000000' ]; then
    FINALIZED=1
    write_zero_reward
    exit 0
fi

export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_INDEX=1
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$TMP/home" \
    TMPDIR="$TMP" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    "$PYTHON_BIN" -I -m venv "$TMP/venv"

env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$TMP/home" \
    TMPDIR="$TMP" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    "$TMP/venv/bin/python" -I -m pip install \
        --no-index \
        --find-links "$TESTS_DIR/wheels" \
        --require-hashes \
        -r "$TESTS_DIR/requirements-test.txt" \
        >"$TMP/install.log" 2>&1

COLLECT_FILE=${COLLECT_FILE:-$TMP/collect.txt}
JUNIT_FILE=${JUNIT_FILE:-$TMP/results.xml}
mkdir -p "$(dirname -- "$COLLECT_FILE")" "$(dirname -- "$JUNIT_FILE")"

set +e
env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$TMP/home" \
    TMPDIR="$TMP" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PROGRAM_DIR="$PROGRAM_DIR" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    "$TMP/venv/bin/python" -I -B -m pytest -p no:cacheprovider \
        --collect-only -q "$TESTS_DIR/test_reserve_audit.py" \
        >"$COLLECT_FILE" 2>&1
collect_status=$?
set -e
if [ "$collect_status" -ne 0 ]; then
    cat "$COLLECT_FILE" >&2
    FINALIZED=1
    write_zero_reward
    exit 2
fi

set +e
env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$TMP/home" \
    TMPDIR="$TMP" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PROGRAM_DIR="$PROGRAM_DIR" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    "$TMP/venv/bin/python" -I -B -m pytest -p no:cacheprovider -q \
        "$TESTS_DIR/test_reserve_audit.py" \
        --junitxml="$JUNIT_FILE" \
        >"$TMP/pytest.log" 2>&1
pytest_status=$?
set -e
cat "$TMP/pytest.log" >&2

if [ ! -s "$JUNIT_FILE" ]; then
    FINALIZED=1
    write_zero_reward
    exit 2
fi

set +e
env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$TMP/home" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    "$PYTHON_BIN" -I - "$JUNIT_FILE" "$REWARD_FILE" "$COLLECT_FILE" "$TMP/scoring.json" <<'PYSCORE'
import json
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal

GROUPS = {
    "energy_dispatch": {
        "weight": Decimal("0.30"),
        "tests": [
            "test_duration_weighted_energy_budget_limits_constant_reserve",
            "test_interval_power_bottleneck_limits_sustainable_reserve",
            "test_nonuniform_intervals_require_energy_redistribution",
        ],
    },
    "telemetry_evidence": {
        "weight": Decimal("0.15"),
        "tests": [
            "test_wh_normalization_and_stale_telemetry_review",
            "test_low_quality_evidence_does_not_cap_but_requires_review",
        ],
    },
    "n1_reliability": {
        "weight": Decimal("0.26"),
        "tests": [
            "test_n_minus_one_enumerates_each_asset_and_tie_breaks_by_id",
            "test_n_minus_one_combines_with_time_varying_power_caps",
        ],
    },
    "feeder_decision": {
        "weight": Decimal("0.15"),
        "tests": [
            "test_feeder_isolation_export_limit_and_requirement_priority",
            "test_insufficient_precedes_review_and_report_sorting_is_deterministic",
        ],
    },
    "validation_atomicity": {
        "weight": Decimal("0.14"),
        "tests": [
            "test_rejects_missing_duplicate_and_unknown_interval_telemetry_without_side_effects",
            "test_rejects_nonfinite_boolean_and_range_values_without_side_effects",
            "test_success_schema_atomic_replace_and_no_temporary_files",
        ],
    },
}
expected = [name for group in GROUPS.values() for name in group["tests"]]
results = {}
hard_gate = "PASS"
try:
    root = ET.parse(sys.argv[1]).getroot()
    for case in root.findall(".//testcase"):
        name = case.attrib.get("name", "")
        if name in results:
            hard_gate = "FAIL: duplicate testcase"
        bad = case.find("failure") is not None or case.find("error") is not None
        skipped = case.find("skipped") is not None
        results[name] = "FAIL" if (bad or skipped) else "PASS"
    if sorted(results) != sorted(expected) or len(results) != len(expected):
        hard_gate = "FAIL: collected test set mismatch"
except Exception:
    hard_gate = "FAIL: missing or malformed junit xml"
try:
    collected_text = open(sys.argv[3], encoding="utf-8").read()
    if not all(name in collected_text for name in expected):
        hard_gate = "FAIL: collect-only set mismatch"
except Exception:
    hard_gate = "FAIL: missing collect-only output"
report = {"formula_version": "behavior-groups-v1", "hard_gate": hard_gate, "groups": [], "reward": "0.000000"}
if hard_gate == "PASS":
    total = Decimal("0")
    for group_id, group in GROUPS.items():
        passed = sum(results.get(name) == "PASS" for name in group["tests"])
        score = Decimal(passed) / Decimal(len(group["tests"]))
        total += group["weight"] * score
        report["groups"].append({"id": group_id, "weight": str(group["weight"]), "passed": passed, "total": len(group["tests"]), "score": f"{score:.6f}", "tests": group["tests"]})
    report["reward"] = f"{total:.6f}"
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    stream.write(report["reward"] + "\n")
with open(sys.argv[4], "w", encoding="utf-8") as stream:
    json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
    stream.write("\n")
raise SystemExit(0 if hard_gate == "PASS" else 1)
PYSCORE
score_status=$?
set -e
if [ "$score_status" -ne 0 ]; then
    FINALIZED=1
    write_zero_reward
    exit 2
fi

if ! "$PYTHON_BIN" -I - "$REWARD_FILE" <<'PYCHECK'
import math
import sys
text = open(sys.argv[1], encoding="ascii").read().strip()
value = float(text)
assert text == "0.000000" or ("." in text and text == f"{value:.6f}")
assert math.isfinite(value) and 0.0 <= value <= 1.0
PYCHECK
then
    FINALIZED=1
    write_zero_reward
    exit 2
fi

FINALIZED=1
exit "$pytest_status"
