import copy
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROGRAM_DIR = Path(os.environ.get("PROGRAM_DIR", Path(__file__).resolve().parents[1] / "environment"))

def asset(ident, feeder="north", capacity=120, soc=1, minimum=0, maximum=100, availability=1):
    return {"id": ident, "feeder_id": feeder, "capacity_kwh": capacity, "soc_fraction": soc, "min_soc_fraction": minimum, "max_discharge_kw": maximum, "availability_fraction": availability}

def reading(asset_id, interval_id, energy=100, unit="kWh", quality=.95, age=0):
    return {"asset_id": asset_id, "interval_id": interval_id, "discharge_energy": energy, "energy_unit": unit, "quality": quality, "age_minutes": age}

def payload(*, max_age=15, request=0, intervals=None, feeders=None, assets=None, telemetry=None):
    intervals = intervals or [{"id": "t1", "duration_minutes": 60}, {"id": "t2", "duration_minutes": 60}]
    feeders = feeders or [{"id": "north", "export_limit_kw": 500, "required_reserve_kw": 0, "n_minus_one": False}]
    assets = assets or [asset("A")]
    telemetry = telemetry or [reading(a["id"], t["id"], energy=1000) for a in assets for t in intervals]
    return {"max_telemetry_age_minutes": max_age, "reserve_request_kw": request, "audit_intervals": intervals, "feeders": feeders, "assets": assets, "telemetry": telemetry}

def run(data, prior=None):
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        input_path, output_path = directory / "input.json", directory / "report.json"
        input_path.write_text(json.dumps(data, allow_nan=True), encoding="utf-8")
        if prior is not None:
            output_path.write_bytes(prior)
        completed = subprocess.run([sys.executable, "-m", "reserve_audit", "--input", str(input_path), "--output", str(output_path)], cwd=PROGRAM_DIR, capture_output=True, text=True)
        raw = output_path.read_bytes() if output_path.exists() else None
        decoded = json.loads(raw) if completed.returncode == 0 else None
        return completed, decoded, raw, sorted(item.name for item in directory.iterdir())

def feeder(result, ident="north"):
    return next(item for item in result["feeders"] if item["id"] == ident)

def close(actual, expected, case_id):
    assert math.isclose(actual, expected, abs_tol=1e-6), f"{case_id}: actual={actual!r}, expected={expected!r}"


def test_duration_weighted_energy_budget_limits_constant_reserve():
    """CASE-ENERGY-001 [R-ENERGY-01, R-DISPATCH-01]：题面原文“对每个单元 a：所有时段 dispatch[a,t] × duration_minutes[t] / 60 <= usable_energy_kwh[a]”。

    连续两个 60 分钟时段共 2 小时；可用能量 60 kWh 时，恒定备用上限应为 30 kW，而不是逐时段各用 60 kWh。
    """
    data = payload(assets=[asset("A", capacity=60, maximum=100)], request=30)
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-ENERGY-001: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-ENERGY-001: no JSON report"
    close(feeder(report)["normal_sustainable_kw"], 30, "CASE-ENERGY-001")
    close(report["fleet_firm_kw"], 30, "CASE-ENERGY-001")
    assert report["status"] == "READY", f"CASE-ENERGY-001: unexpected status {report['status']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-ENERGY-001: unexpected files {entries!r}"


def test_interval_power_bottleneck_limits_sustainable_reserve():
    """CASE-POWER-002 [R-POWER-01, R-DISPATCH-01]：题面原文“dispatch[a,t] <= 该单元在 t 的 power_cap_kw”，任一时段都可成为持续承诺瓶颈。

    单元在 t1 有 100 kW 可信功率、在 t2 只有 20 kW；恒定承诺必须被 t2 限制为 20 kW。
    """
    data = payload(assets=[asset("A", capacity=500, maximum=100)], telemetry=[reading("A", "t1", 100), reading("A", "t2", 20)], request=20)
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-POWER-002: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-POWER-002: no JSON report"
    close(feeder(report)["normal_sustainable_kw"], 20, "CASE-POWER-002")
    intervals = report["assets"][0]["intervals"]
    close(intervals[0]["power_cap_kw"], 100, "CASE-POWER-002")
    close(intervals[1]["power_cap_kw"], 20, "CASE-POWER-002")
    assert report["status"] == "REVIEW", f"CASE-POWER-002: unexpected status {report['status']!r}"
    assert report["review_assets"] == ["A"], f"CASE-POWER-002: unexpected review list {report['review_assets']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-POWER-002: unexpected files {entries!r}"


def test_nonuniform_intervals_require_energy_redistribution():
    """CASE-REDIST-003 [R-DISPATCH-01, R-ENERGY-01]：题面原文“只有在存在一组非负调度量 dispatch[a,t] 时，P 才可持续”，必须跨不同长度时段重分配能量。

    聚合能量上界给 62.5 kW，但 B 在 90 分钟尾段无功率；A 必须在尾段单独承担 50 kW，故真实上限是 50 kW。
    """
    intervals = [{"id": "peak", "duration_minutes": 30}, {"id": "tail", "duration_minutes": 90}]
    data = payload(intervals=intervals, assets=[asset("A", capacity=75, maximum=100), asset("B", capacity=50, maximum=100)], telemetry=[reading("A", "peak", 50), reading("A", "tail", 150), reading("B", "peak", 50), reading("B", "tail", 0)], request=50)
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-REDIST-003: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-REDIST-003: no JSON report"
    close(feeder(report)["normal_sustainable_kw"], 50, "CASE-REDIST-003")
    assert report["status"] == "REVIEW", f"CASE-REDIST-003: unexpected status {report['status']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-REDIST-003: unexpected files {entries!r}"


def test_wh_normalization_and_stale_telemetry_review():
    """CASE-TELEMETRY-004 [R-TELEM-01, R-TELEM-02, R-TELEM-03]：题面原文“每条遥测先把 discharge_energy 转为 kWh，再按该审计时段的时长计算”；超时效遥测不可信但只触发复核。

    t2 的 40000 Wh 在 60 分钟后为 40 kW；t1 虽为高质量但 age=6 超过 max_age=5，因此 telemetry_limited_kw 为 null、功率上限退回额定 80 kW。
    """
    data = payload(max_age=5, assets=[asset("A", capacity=500, maximum=80)], telemetry=[reading("A", "t1", 40000, "Wh", .95, 6), reading("A", "t2", 40000, "Wh", .95, 0)])
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-TELEMETRY-004: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-TELEMETRY-004: no JSON report"
    intervals = report["assets"][0]["intervals"]
    assert intervals[0]["telemetry_limited_kw"] is None, f"CASE-TELEMETRY-004: stale telemetry was trusted: {intervals[0]!r}"
    close(intervals[0]["power_cap_kw"], 80, "CASE-TELEMETRY-004")
    close(intervals[1]["telemetry_limited_kw"], 40, "CASE-TELEMETRY-004")
    close(intervals[1]["power_cap_kw"], 40, "CASE-TELEMETRY-004")
    assert report["review_assets"] == ["A"], f"CASE-TELEMETRY-004: unexpected review list {report['review_assets']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-TELEMETRY-004: unexpected files {entries!r}"


def test_low_quality_evidence_does_not_cap_but_requires_review():
    """CASE-REVIEW-005 [R-TELEM-02, R-TELEM-03]：题面原文“不可信时 telemetry_limited_kw 为 null，power_cap_kw 即 nameplate_limited_kw”且“复核不等同于把单元从容量中删除”。

    quality=0.89 低于 0.90 阈值，因此不能以 1 kWh 的观测压低容量，但该单元必须进入 review_assets。
    """
    data = payload(assets=[asset("A", capacity=200, maximum=70)], telemetry=[reading("A", "t1", 1, quality=.89), reading("A", "t2", 1, quality=.89)])
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-REVIEW-005: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-REVIEW-005: no JSON report"
    close(feeder(report)["normal_sustainable_kw"], 70, "CASE-REVIEW-005")
    assert report["review_assets"] == ["A"], f"CASE-REVIEW-005: low-quality evidence not reviewed: {report['review_assets']!r}"
    assert report["status"] == "REVIEW", f"CASE-REVIEW-005: unexpected status {report['status']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-REVIEW-005: unexpected files {entries!r}"


def test_n_minus_one_enumerates_each_asset_and_tie_breaks_by_id():
    """CASE-N1-ENUM-006 [R-N1-01, R-N1-02]：题面原文“必须分别假设该馈线的每一个单元永久失效”且“并列时取字典序最小 ID”。

    A 是额定功率最大的单元，但 A 失效后仍有 100 kW；B 或 C 失效才把持续能力压到 60 kW，并列结果必须选择 B 而不是 C。
    """
    assets = [asset("A", capacity=20, maximum=200), asset("B", capacity=100, maximum=100), asset("C", capacity=100, maximum=100)]
    data = payload(feeders=[{"id": "north", "export_limit_kw": 500, "required_reserve_kw": 0, "n_minus_one": True}], assets=assets, request=20)
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-N1-ENUM-006: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-N1-ENUM-006: no JSON report"
    f = feeder(report)
    close(f["normal_sustainable_kw"], 110, "CASE-N1-ENUM-006")
    close(f["contingency_sustainable_kw"], 60, "CASE-N1-ENUM-006")
    assert f["contingency_asset_id"] == "B", f"CASE-N1-ENUM-006: wrong tie-break victim {f['contingency_asset_id']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-N1-ENUM-006: unexpected files {entries!r}"


def test_n_minus_one_combines_with_time_varying_power_caps():
    """CASE-N1-TIME-007 [R-N1-01, R-DISPATCH-01]：题面原文“分别假设该馈线的每一个单元永久失效，并计算剩余单元的最大可持续 P”，故障后必须重算全部时段的功率证据。

    A 的额定功率最大，但 C 是第二时段唯一仍可提供功率的支撑；移除 C 后剩余单元只剩 20 kW，故故障元凶是 C 且值为 20 kW。
    """
    assets = [asset("A", capacity=500, maximum=200), asset("B", capacity=500, maximum=150), asset("C", capacity=500, maximum=100)]
    telemetry = [reading("A", "t1", 200), reading("A", "t2", 20), reading("B", "t1", 150), reading("B", "t2", 0), reading("C", "t1", 0), reading("C", "t2", 100)]
    data = payload(feeders=[{"id": "north", "export_limit_kw": 500, "required_reserve_kw": 0, "n_minus_one": True}], assets=assets, telemetry=telemetry)
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-N1-TIME-007: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-N1-TIME-007: no JSON report"
    f = feeder(report)
    close(f["contingency_sustainable_kw"], 20, "CASE-N1-TIME-007")
    assert f["contingency_asset_id"] == "C", f"CASE-N1-TIME-007: wrong contingency asset {f['contingency_asset_id']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-N1-TIME-007: unexpected files {entries!r}"


def test_feeder_isolation_export_limit_and_requirement_priority():
    """CASE-FEEDER-008 [R-FEEDER-01, R-FEEDER-02, R-STATUS-01]：题面原文“每条馈线都必须用自身的 firm_kw 与 required_reserve_kw 判断”且“不得把能量或功率跨馈线转移”。

    north 的 firm_kw=30 低于本地需求 40，虽然 south 有 100 kW 且车队合计 130 kW 超过总请求 100 kW，最终仍必须为 INSUFFICIENT。
    """
    assets = [asset("A", "north", capacity=300, maximum=100), asset("B", "south", capacity=300, maximum=100)]
    feeders = [{"id": "north", "export_limit_kw": 30, "required_reserve_kw": 40, "n_minus_one": False}, {"id": "south", "export_limit_kw": 100, "required_reserve_kw": 0, "n_minus_one": False}]
    data = payload(feeders=feeders, assets=assets, request=100)
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-FEEDER-008: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-FEEDER-008: no JSON report"
    close(report["fleet_firm_kw"], 130, "CASE-FEEDER-008")
    close(feeder(report, "north")["firm_kw"], 30, "CASE-FEEDER-008")
    assert feeder(report, "north")["meets_requirement"] is False, "CASE-FEEDER-008: north local shortage was missed"
    assert feeder(report, "south")["meets_requirement"] is True, "CASE-FEEDER-008: south local requirement was misreported"
    assert report["status"] == "INSUFFICIENT", f"CASE-FEEDER-008: unexpected status {report['status']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-FEEDER-008: unexpected files {entries!r}"


def test_insufficient_precedes_review_and_report_sorting_is_deterministic():
    """CASE-STATUS-009 [R-STATUS-01, R-OUT-01]：题面原文“若总请求未满足，或任一馈线不满足自身需求，则为 INSUFFICIENT；否则若 review_assets 非空则为 REVIEW”。

    输入同时触发容量不足与低质量复核时，INSUFFICIENT 必须优先；assets 与 review_assets 都按字典序稳定排列。
    """
    assets = [asset("z", capacity=100, maximum=50), asset("a", capacity=100, maximum=50)]
    data = payload(request=200, assets=assets, telemetry=[reading("z", "t1", 1, quality=.5), reading("z", "t2", 1, quality=.5), reading("a", "t1", 1, quality=.5), reading("a", "t2", 1, quality=.5)])
    p, report, raw, entries = run(data)
    assert p.returncode == 0, f"CASE-STATUS-009: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-STATUS-009: no JSON report"
    assert report["status"] == "INSUFFICIENT", f"CASE-STATUS-009: REVIEW masked INSUFFICIENT: {report['status']!r}"
    assert [item["id"] for item in report["assets"]] == ["a", "z"], f"CASE-STATUS-009: assets not sorted: {[item['id'] for item in report['assets']]!r}"
    assert report["review_assets"] == ["a", "z"], f"CASE-STATUS-009: review list not sorted: {report['review_assets']!r}"
    assert entries == ["input.json", "report.json"], f"CASE-STATUS-009: unexpected files {entries!r}"


def test_rejects_missing_duplicate_and_unknown_interval_telemetry_without_side_effects():
    """CASE-VALID-TELEMETRY-010 [R-INPUT-02, R-INPUT-03, R-ERROR-01]：题面原文“每个 (asset_id, interval_id) 组合恰好一条记录”以及“未知引用、重复或缺失的 ID/遥测组合……均须拒收”。

    缺记录、重复组合和未知时段引用三条拒收路径都必须以 INPUT_ERROR 退出，且旧报告字节与目录清单逐字节保持不变。
    """
    good = payload()
    cases = []
    missing = copy.deepcopy(good); missing["telemetry"].pop(); cases.append(("missing-combination", missing))
    duplicate = copy.deepcopy(good); duplicate["telemetry"].append(copy.deepcopy(duplicate["telemetry"][0])); cases.append(("duplicate-combination", duplicate))
    unknown = copy.deepcopy(good); unknown["telemetry"][0]["interval_id"] = "none"; cases.append(("unknown-interval", unknown))
    for label, case in cases:
        p, _, raw, entries = run(case, b"previous\x00report")
        assert p.returncode != 0, f"CASE-VALID-TELEMETRY-010[{label}]: invalid input accepted"
        assert p.stderr.startswith("INPUT_ERROR:"), f"CASE-VALID-TELEMETRY-010[{label}]: wrong stderr {p.stderr!r}"
        assert raw == b"previous\x00report", f"CASE-VALID-TELEMETRY-010[{label}]: prior report changed"
        assert entries == ["input.json", "report.json"], f"CASE-VALID-TELEMETRY-010[{label}]: unexpected files {entries!r}"


def test_rejects_nonfinite_boolean_and_range_values_without_side_effects():
    """CASE-VALID-NUMBER-011 [R-INPUT-01, R-INPUT-04, R-ERROR-01]：题面原文“错误类型、缺失键、非法枚举、布尔值代替数值、非有限数与越界值均须拒收”。

    布尔冒充整数/数值、NaN、无穷大、越界 SOC、非法的 n_minus_one、负 age 与非整数 age 都必须 fail-closed，且不触碰既有报告。
    """
    cases = [
        ("boolean-max-age", payload(max_age=True)),
        ("boolean-request", payload(request=True)),
        ("boolean-capacity", payload(assets=[asset("A", capacity=True)])),
        ("nonfinite-request", payload(request=float("nan"))),
        ("nonfinite-energy", payload(telemetry=[reading("A", "t1", energy=float("inf")), reading("A", "t2")])),
        ("range-soc", payload(assets=[asset("A", soc=1.1)])),
        ("range-min-soc", payload(assets=[asset("A", soc=.5, minimum=.8)])),
        ("bad-boolean-enum", payload(feeders=[{"id": "north", "export_limit_kw": 500, "required_reserve_kw": 0, "n_minus_one": 1}])),
        ("negative-age", payload(telemetry=[reading("A", "t1", age=-1), reading("A", "t2")])),
        ("noninteger-age", payload(telemetry=[reading("A", "t1", age=1.5), reading("A", "t2")])),
    ]
    for label, case in cases:
        p, _, raw, entries = run(case, b"KEEP")
        assert p.returncode != 0, f"CASE-VALID-NUMBER-011[{label}]: invalid input accepted"
        assert p.stderr.startswith("INPUT_ERROR:"), f"CASE-VALID-NUMBER-011[{label}]: wrong stderr {p.stderr!r}"
        assert raw == b"KEEP", f"CASE-VALID-NUMBER-011[{label}]: prior report changed"
        assert entries == ["input.json", "report.json"], f"CASE-VALID-NUMBER-011[{label}]: unexpected files {entries!r}"


def test_success_schema_atomic_replace_and_no_temporary_files():
    """CASE-ATOMIC-012 [R-ATOMIC-01, R-OUT-01]：题面原文“成功时以 UTF-8 JSON 原子替换输出文件”且顶层字段必须为指定集合。

    成功运行必须替换旧字节、写出完整顶层字段和末尾换行，并且目录中只能留下输入与最终报告，不得遗留临时文件。
    """
    p, report, raw, entries = run(payload(), b"old")
    assert p.returncode == 0, f"CASE-ATOMIC-012: programme failed: {p.stderr!r}"
    assert report is not None and raw is not None, "CASE-ATOMIC-012: no JSON report"
    assert raw != b"old", "CASE-ATOMIC-012: prior report was not replaced"
    assert raw.endswith(b"\n"), f"CASE-ATOMIC-012: report lacks trailing newline: {raw[-16:]!r}"
    assert set(report) == {"status", "max_telemetry_age_minutes", "reserve_request_kw", "fleet_firm_kw", "assets", "feeders", "review_assets"}, f"CASE-ATOMIC-012: schema mismatch {sorted(report)!r}"
    assert entries == ["input.json", "report.json"], f"CASE-ATOMIC-012: temporary file leaked: {entries!r}"
