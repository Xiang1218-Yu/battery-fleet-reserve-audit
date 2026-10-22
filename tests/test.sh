#!/bin/sh
set -u
TESTS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$TESTS_DIR" = "/tests" ]; then
  REWARD_FILE=${REWARD_FILE:-/logs/verifier/reward.txt}
  export PROGRAM_DIR=${PROGRAM_DIR:-/app}
else
  ROOT=$(CDPATH= cd -- "$TESTS_DIR/.." && pwd)
  REWARD_FILE=${REWARD_FILE:-"$ROOT/reward.txt"}
  export PROGRAM_DIR=${PROGRAM_DIR:-"$ROOT/environment"}
fi
mkdir -p "$(dirname -- "$REWARD_FILE")"
printf '0.000000\n' > "$REWARD_FILE"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
export PYTHONDONTWRITEBYTECODE=1
python3 -m venv "$TMP/venv" || exit 1
"$TMP/venv/bin/python" -m pip install --no-index --find-links "$TESTS_DIR/wheels" --require-hashes -r "$TESTS_DIR/requirements-test.txt" || exit 1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$TMP/venv/bin/python" -m pytest --collect-only -q "$TESTS_DIR/test_reserve_audit.py" >"$TMP/collect.txt" 2>&1 || exit 1
set +e
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$TMP/venv/bin/python" -m pytest -p no:cacheprovider -q "$TESTS_DIR/test_reserve_audit.py" --junitxml="$TMP/results.xml"
status=$?
set -e
python3 - "$TMP/results.xml" "$REWARD_FILE" <<'PY2'
import sys,xml.etree.ElementTree as ET
weights={'test_duration_weighted_energy_budget_limits_constant_reserve':.10,'test_interval_power_bottleneck_limits_sustainable_reserve':.09,'test_nonuniform_intervals_require_energy_redistribution':.11,'test_wh_normalization_and_stale_telemetry_review':.08,'test_low_quality_evidence_does_not_cap_but_requires_review':.07,'test_n_minus_one_enumerates_each_asset_and_tie_breaks_by_id':.13,'test_n_minus_one_combines_with_time_varying_power_caps':.13,'test_feeder_isolation_export_limit_and_requirement_priority':.09,'test_insufficient_precedes_review_and_report_sorting_is_deterministic':.06,'test_rejects_missing_duplicate_and_unknown_interval_telemetry_without_side_effects':.06,'test_rejects_nonfinite_boolean_and_range_values_without_side_effects':.05,'test_success_schema_atomic_replace_and_no_temporary_files':.03}
score=0.0
try:
 root=ET.parse(sys.argv[1]).getroot(); names=[]
 for case in root.findall('.//testcase'):
  name=case.attrib.get('name',''); names.append(name)
  if case.find('failure') is None and case.find('error') is None and case.find('skipped') is None: score += weights.get(name,0)
 if set(names)!=set(weights) or len(names)!=len(weights): score=0.0
except Exception: score=0.0
with open(sys.argv[2],'w',encoding='utf-8') as stream: stream.write(f'{score:.6f}\n')
PY2
exit "$status"
