import argparse
import json
import math
import os
import tempfile
from collections import deque
from pathlib import Path

EPS = 1e-8

class InputError(ValueError):
    pass

def number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise InputError(f"{name} must be a finite number")
    if not math.isfinite(result):
        raise InputError(f"{name} must be a finite number")
    if positive and result <= 0:
        raise InputError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise InputError(f"{name} must be nonnegative")
    return result

def integer(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{name} must be an integer")
    if positive and value <= 0:
        raise InputError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise InputError(f"{name} must be nonnegative")
    return value

def text(value, name):
    if not isinstance(value, str) or not value:
        raise InputError(f"{name} must be a nonempty string")
    return value

def obj(value, name):
    if not isinstance(value, dict):
        raise InputError(f"{name} must be an object")
    return value

def array(value, name):
    if not isinstance(value, list) or not value:
        raise InputError(f"{name} must be a nonempty array")
    return value

def required(item, keys, name):
    for key in keys:
        if key not in item:
            raise InputError(f"{name}.{key} is required")

def rounded(value):
    return round(value, 8)

class Flow:
    def __init__(self, size):
        self.graph = [[] for _ in range(size)]
    def add(self, left, right, capacity):
        self.graph[left].append([right, capacity, len(self.graph[right])])
        self.graph[right].append([left, 0.0, len(self.graph[left]) - 1])
    def maximum(self, source, sink):
        total = 0.0
        while True:
            level = [-1] * len(self.graph)
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for target, capacity, _ in self.graph[node]:
                    if capacity > EPS and level[target] < 0:
                        level[target] = level[node] + 1
                        queue.append(target)
            if level[sink] < 0:
                return total
            cursor = [0] * len(self.graph)
            def send(node, amount):
                if node == sink:
                    return amount
                while cursor[node] < len(self.graph[node]):
                    edge = self.graph[node][cursor[node]]
                    target, capacity, reverse = edge
                    if capacity > EPS and level[target] == level[node] + 1:
                        pushed = send(target, min(amount, capacity))
                        if pushed > EPS:
                            edge[1] -= pushed
                            self.graph[target][reverse][1] += pushed
                            return pushed
                    cursor[node] += 1
                return 0.0
            while True:
                pushed = send(source, float("inf"))
                if pushed <= EPS:
                    break
                total += pushed

def parse(data):
    data = obj(data, "input")
    required(data, ["max_telemetry_age_minutes", "reserve_request_kw", "audit_intervals", "feeders", "assets", "telemetry"], "input")
    max_age = integer(data["max_telemetry_age_minutes"], "max_telemetry_age_minutes", positive=True)
    request = number(data["reserve_request_kw"], "reserve_request_kw", nonnegative=True)
    intervals = {}
    ordered_interval_ids = []
    for raw in array(data["audit_intervals"], "audit_intervals"):
        raw = obj(raw, "audit_interval")
        required(raw, ["id", "duration_minutes"], "audit_interval")
        ident = text(raw["id"], "audit_interval.id")
        if ident in intervals:
            raise InputError("duplicate audit interval id")
        intervals[ident] = {"id": ident, "duration": integer(raw["duration_minutes"], "duration_minutes", positive=True)}
        ordered_interval_ids.append(ident)
    feeders = {}
    for raw in array(data["feeders"], "feeders"):
        raw = obj(raw, "feeder")
        required(raw, ["id", "export_limit_kw", "required_reserve_kw", "n_minus_one"], "feeder")
        ident = text(raw["id"], "feeder.id")
        if ident in feeders:
            raise InputError("duplicate feeder id")
        if not isinstance(raw["n_minus_one"], bool):
            raise InputError("feeder.n_minus_one must be boolean")
        feeders[ident] = {"id": ident, "export": number(raw["export_limit_kw"], "export_limit_kw", positive=True), "required": number(raw["required_reserve_kw"], "required_reserve_kw", nonnegative=True), "n1": raw["n_minus_one"]}
    assets = {}
    for raw in array(data["assets"], "assets"):
        raw = obj(raw, "asset")
        required(raw, ["id", "feeder_id", "capacity_kwh", "soc_fraction", "min_soc_fraction", "max_discharge_kw", "availability_fraction"], "asset")
        ident = text(raw["id"], "asset.id")
        feeder = text(raw["feeder_id"], "asset.feeder_id")
        if ident in assets:
            raise InputError("duplicate asset id")
        if feeder not in feeders:
            raise InputError("asset references unknown feeder")
        soc = number(raw["soc_fraction"], "soc_fraction", nonnegative=True)
        minimum = number(raw["min_soc_fraction"], "min_soc_fraction", nonnegative=True)
        availability = number(raw["availability_fraction"], "availability_fraction", positive=True)
        if soc > 1 or minimum > 1 or minimum > soc or availability > 1:
            raise InputError("asset fraction out of range")
        assets[ident] = {"id": ident, "feeder_id": feeder, "energy": number(raw["capacity_kwh"], "capacity_kwh", positive=True) * (soc - minimum), "nameplate": number(raw["max_discharge_kw"], "max_discharge_kw", positive=True) * availability, "caps": {}, "review": False}
    readings = {}
    for raw in array(data["telemetry"], "telemetry"):
        raw = obj(raw, "telemetry")
        required(raw, ["asset_id", "interval_id", "discharge_energy", "energy_unit", "quality", "age_minutes"], "telemetry")
        asset_id = text(raw["asset_id"], "telemetry.asset_id")
        interval_id = text(raw["interval_id"], "telemetry.interval_id")
        if asset_id not in assets:
            raise InputError("telemetry references unknown asset")
        if interval_id not in intervals:
            raise InputError("telemetry references unknown interval")
        key = (asset_id, interval_id)
        if key in readings:
            raise InputError("duplicate telemetry combination")
        unit = raw["energy_unit"]
        if unit not in ("Wh", "kWh"):
            raise InputError("energy_unit is invalid")
        energy = number(raw["discharge_energy"], "discharge_energy", nonnegative=True)
        if unit == "Wh":
            energy /= 1000.0
        quality = number(raw["quality"], "quality", nonnegative=True)
        if quality > 1:
            raise InputError("quality out of range")
        age = integer(raw["age_minutes"], "age_minutes", nonnegative=True)
        observed = energy * 60.0 / intervals[interval_id]["duration"]
        trusted = quality >= 0.90
        readings[key] = (observed, trusted)
    expected = {(asset_id, interval_id) for asset_id in assets for interval_id in intervals}
    if set(readings) != expected:
        raise InputError("each asset needs exactly one telemetry record per interval")
    for (asset_id, interval_id), (observed, trusted) in readings.items():
        asset = assets[asset_id]
        cap = min(asset["nameplate"], observed) if trusted else asset["nameplate"]
        asset["caps"][interval_id] = (observed if trusted else None, cap)
        if trusted and observed < asset["nameplate"] * 0.70:
            asset["review"] = True
    return max_age, request, intervals, ordered_interval_ids, feeders, assets

def sustainable(asset_ids, assets, intervals, ordered_interval_ids):
    asset_ids = sorted(asset_ids)
    if not asset_ids:
        return 0.0
    duration_hours = sum(intervals[interval_id]["duration"] for interval_id in ordered_interval_ids) / 60.0
    power = min(sum(assets[asset_id]["caps"][interval_id][1] for asset_id in asset_ids) for interval_id in ordered_interval_ids)
    energy = sum(assets[asset_id]["energy"] for asset_id in asset_ids) / duration_hours
    return min(power, energy)

def audit(data):
    max_age, request, intervals, ordered_interval_ids, feeders, assets = parse(data)
    asset_results = []
    for asset_id in sorted(assets):
        asset = assets[asset_id]
        interval_results = []
        for interval_id in sorted(intervals):
            telemetry_limit, power_cap = asset["caps"][interval_id]
            interval_results.append({"id": interval_id, "telemetry_limited_kw": None if telemetry_limit is None else rounded(telemetry_limit), "power_cap_kw": rounded(power_cap)})
        asset_results.append({"id": asset_id, "feeder_id": asset["feeder_id"], "usable_energy_kwh": rounded(asset["energy"]), "nameplate_limited_kw": rounded(asset["nameplate"]), "intervals": interval_results})
    feeder_results = []
    for feeder_id in sorted(feeders):
        feeder = feeders[feeder_id]
        members = sorted(asset_id for asset_id, asset in assets.items() if asset["feeder_id"] == feeder_id)
        normal = sustainable(members, assets, intervals, ordered_interval_ids)
        culprit = None
        contingency = normal
        if feeder["n1"] and members:
            contingency = float("inf")
            largest = max(members, key=lambda asset_id: assets[asset_id]["nameplate"])
            contingency = sustainable([asset_id for asset_id in members if asset_id != largest], assets, intervals, ordered_interval_ids)
            culprit = largest
        if not members:
            contingency = 0.0
        firm = min(feeder["export"], contingency)
        feeder_results.append({"id": feeder_id, "normal_sustainable_kw": rounded(normal), "contingency_sustainable_kw": rounded(contingency), "contingency_asset_id": culprit, "firm_kw": rounded(firm), "required_reserve_kw": rounded(feeder["required"]), "meets_requirement": firm + EPS >= feeder["required"]})
    fleet = sum(feeder["firm_kw"] for feeder in feeder_results)
    review = sorted(asset_id for asset_id, asset in assets.items() if asset["review"])
    insufficient = fleet + EPS < request or any(not feeder["meets_requirement"] for feeder in feeder_results)
    return {"status": "REVIEW" if review else ("INSUFFICIENT" if insufficient else "READY"), "max_telemetry_age_minutes": max_age, "reserve_request_kw": rounded(request), "fleet_firm_kw": rounded(fleet), "assets": asset_results, "feeders": feeder_results, "review_assets": review}

def atomic_write(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".reserve-audit-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    try:
        with open(arguments.input, encoding="utf-8") as stream:
            result = audit(json.load(stream))
        atomic_write(arguments.output, result)
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as error:
        print(f"INPUT_ERROR: {error}", file=__import__("sys").stderr)
        raise SystemExit(2)

if __name__ == "__main__":
    main()
