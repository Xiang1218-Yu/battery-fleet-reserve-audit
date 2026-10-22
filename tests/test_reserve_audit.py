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

def close(actual, expected):
    assert math.isclose(actual, expected, abs_tol=1e-6), (actual, expected)

def test_duration_weighted_energy_budget_limits_constant_reserve():
    """跨时段条款：能量预算必须按每段时长耦合，而非逐段独立使用。"""
    data = payload(assets=[asset("A", capacity=60, maximum=100)], request=30)
    p, report, _, _ = run(data)
    assert p.returncode == 0
    close(feeder(report)["normal_sustainable_kw"], 30)
    close(report["fleet_firm_kw"], 30)
    assert report["status"] == "READY"

def test_interval_power_bottleneck_limits_sustainable_reserve():
    """跨时段条款：任一时段的可信功率限额都可成为持续承诺瓶颈。"""
    data = payload(assets=[asset("A", capacity=500, maximum=100)], telemetry=[reading("A", "t1", 100), reading("A", "t2", 20)], request=20)
    p, report, _, _ = run(data)
    assert p.returncode == 0
    close(feeder(report)["normal_sustainable_kw"], 20)
    assert report["status"] == "REVIEW" and report["review_assets"] == ["A"]

def test_nonuniform_intervals_require_energy_redistribution():
    """跨时段条款：不同长度时段下，有限能量应在可用功率约束内重新分配。"""
    intervals = [{"id": "peak", "duration_minutes": 30}, {"id": "tail", "duration_minutes": 90}]
    data = payload(intervals=intervals, assets=[asset("A", capacity=75, maximum=100), asset("B", capacity=50, maximum=100)], telemetry=[reading("A", "peak", 50), reading("A", "tail", 150), reading("B", "peak", 50), reading("B", "tail", 0)], request=50)
    p, report, _, _ = run(data)
    assert p.returncode == 0
    # Aggregate energy permits 62.5 kW, but A alone must sustain the 90-minute tail.
    close(feeder(report)["normal_sustainable_kw"], 50)
    assert report["status"] == "REVIEW"

def test_wh_normalization_and_stale_telemetry_review():
    """证据条款：Wh 必须转换；超过时效的高质量遥测不降额但需要复核。"""
    data = payload(max_age=5, assets=[asset("A", capacity=500, maximum=80)], telemetry=[reading("A", "t1", 40000, "Wh", .95, 6), reading("A", "t2", 40000, "Wh", .95, 0)])
    p, report, _, _ = run(data)
    assert p.returncode == 0
    intervals = report["assets"][0]["intervals"]
    assert intervals[0]["telemetry_limited_kw"] is None
    close(intervals[0]["power_cap_kw"], 80)
    close(intervals[1]["telemetry_limited_kw"], 40)
    assert report["review_assets"] == ["A"]

def test_low_quality_evidence_does_not_cap_but_requires_review():
    """证据条款：低质量遥测不应直接压低容量，但必须标记复核。"""
    data = payload(assets=[asset("A", capacity=200, maximum=70)], telemetry=[reading("A", "t1", 1, quality=.89), reading("A", "t2", 1, quality=.89)])
    p, report, _, _ = run(data)
    assert p.returncode == 0
    close(feeder(report)["normal_sustainable_kw"], 70)
    assert report["review_assets"] == ["A"] and report["status"] == "REVIEW"

def test_n_minus_one_enumerates_each_asset_and_tie_breaks_by_id():
    """可靠性条款：N-1 必须实际枚举单元失效；最差情景并列时取字典序最小 ID。"""
    assets = [asset("A", capacity=20, maximum=200), asset("B", capacity=100, maximum=100), asset("C", capacity=100, maximum=100)]
    data = payload(feeders=[{"id": "north", "export_limit_kw": 500, "required_reserve_kw": 0, "n_minus_one": True}], assets=assets, request=20)
    p, report, _, _ = run(data)
    assert p.returncode == 0
    f = feeder(report)
    close(f["normal_sustainable_kw"], 110)
    # B 和 C 失效都将结果降为 60；不能只删除额定功率最大的 A。
    close(f["contingency_sustainable_kw"], 60)
    assert f["contingency_asset_id"] == "B"

def test_n_minus_one_combines_with_time_varying_power_caps():
    """可靠性条款：故障后的持续承诺必须重新按所有时段功率证据计算。"""
    assets = [asset("A", capacity=500, maximum=200), asset("B", capacity=500, maximum=150), asset("C", capacity=500, maximum=100)]
    telemetry = [reading("A", "t1", 200), reading("A", "t2", 20), reading("B", "t1", 150), reading("B", "t2", 0), reading("C", "t1", 0), reading("C", "t2", 100)]
    data = payload(feeders=[{"id": "north", "export_limit_kw": 500, "required_reserve_kw": 0, "n_minus_one": True}], assets=assets, telemetry=telemetry)
    p, report, _, _ = run(data)
    assert p.returncode == 0
    f = feeder(report)
    # A 的额定功率最大，但 C 是第二时段的唯一有效支撑。
    close(f["contingency_sustainable_kw"], 20)
    assert f["contingency_asset_id"] == "C"

def test_feeder_isolation_export_limit_and_requirement_priority():
    """系统条款：馈线不可互转；本地需求失败即使总量足够也必须容量不足。"""
    assets = [asset("A", "north", capacity=300, maximum=100), asset("B", "south", capacity=300, maximum=100)]
    feeders = [{"id": "north", "export_limit_kw": 30, "required_reserve_kw": 40, "n_minus_one": False}, {"id": "south", "export_limit_kw": 100, "required_reserve_kw": 0, "n_minus_one": False}]
    data = payload(feeders=feeders, assets=assets, request=100)
    p, report, _, _ = run(data)
    assert p.returncode == 0
    close(report["fleet_firm_kw"], 130)
    assert not feeder(report, "north")["meets_requirement"]
    assert report["status"] == "INSUFFICIENT"

def test_insufficient_precedes_review_and_report_sorting_is_deterministic():
    """结论条款：容量不足优先于复核，且所有报告集合应稳定排序。"""
    assets = [asset("z", capacity=100, maximum=50), asset("a", capacity=100, maximum=50)]
    data = payload(request=200, assets=assets, telemetry=[reading("z", "t1", 1, quality=.5), reading("z", "t2", 1, quality=.5), reading("a", "t1", 1, quality=.5), reading("a", "t2", 1, quality=.5)])
    p, report, _, _ = run(data)
    assert p.returncode == 0 and report["status"] == "INSUFFICIENT"
    assert [item["id"] for item in report["assets"]] == ["a", "z"]
    assert report["review_assets"] == ["a", "z"]

def test_rejects_missing_duplicate_and_unknown_interval_telemetry_without_side_effects():
    """结构条款：遥测矩阵必须完整且唯一，错误引用必须拒收并保留旧报告。"""
    good = payload()
    cases = []
    missing = copy.deepcopy(good); missing["telemetry"].pop(); cases.append(missing)
    duplicate = copy.deepcopy(good); duplicate["telemetry"].append(copy.deepcopy(duplicate["telemetry"][0])); cases.append(duplicate)
    unknown = copy.deepcopy(good); unknown["telemetry"][0]["interval_id"] = "none"; cases.append(unknown)
    for case in cases:
        p, _, raw, entries = run(case, b"previous\x00report")
        assert p.returncode != 0 and p.stderr.startswith("INPUT_ERROR:")
        assert raw == b"previous\x00report" and entries == ["input.json", "report.json"]

def test_rejects_nonfinite_boolean_and_range_values_without_side_effects():
    """数值条款：布尔、非有限数和范围外数据不能进入容量计算或改写报告。"""
    cases = [payload(max_age=True), payload(request=float("nan")), payload(assets=[asset("A", soc=1.1)]), payload(telemetry=[reading("A", "t1", age=-1), reading("A", "t2")])]
    for case in cases:
        p, _, raw, entries = run(case, b"KEEP")
        assert p.returncode != 0 and p.stderr.startswith("INPUT_ERROR:") and raw == b"KEEP" and entries == ["input.json", "report.json"]

def test_success_schema_atomic_replace_and_no_temporary_files():
    """持久化条款：成功报告字段完整，并原子替换旧报告且不遗留临时文件。"""
    p, report, _, entries = run(payload(), b"old")
    assert p.returncode == 0
    assert set(report) == {"status", "max_telemetry_age_minutes", "reserve_request_kw", "fleet_firm_kw", "assets", "feeders", "review_assets"}
    assert entries == ["input.json", "report.json"]
