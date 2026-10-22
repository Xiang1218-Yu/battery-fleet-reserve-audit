# 电池储能车队多时段备用承诺审计

调度中心需要评估一支电池储能车队，是否能在**整个审计窗口的每个时段**持续承诺相同的备用功率。请修复 `environment/reserve_audit` 中的 Python 程序。入口为：

```bash
python3 -m reserve_audit --input INPUT.json --output OUTPUT.json
```

程序不得联网或依赖第三方 Python 包，也不得修改 `tests/`。

## 输入与校验

输入是 JSON 对象，必须包含：

- `max_telemetry_age_minutes`：正整数。超过此时效的遥测不能作为可信功率证据。
- `reserve_request_kw`：非负有限数，车队总备用请求。
- `audit_intervals`：非空数组。每项含唯一非空字符串 `id` 与正整数 `duration_minutes`；它们按输入顺序组成连续审计窗口。
- `feeders`：非空数组。每项含唯一非空字符串 `id`、正有限数 `export_limit_kw`、非负有限数 `required_reserve_kw` 与布尔值 `n_minus_one`。
- `assets`：非空数组。每项含唯一非空字符串 `id`、已有的 `feeder_id`、正有限数 `capacity_kwh`、`soc_fraction`、`min_soc_fraction`、正有限数 `max_discharge_kw` 与 `availability_fraction`。SOC 均为 `[0,1]` 内有限数且 `min_soc_fraction <= soc_fraction`；可用度为 `(0,1]` 内有限数。
- `telemetry`：每个 `(asset_id, interval_id)` 组合**恰好一条**记录。每项含已有的 `asset_id` 和 `interval_id`、非负有限数 `discharge_energy`、`energy_unit`（`"Wh"` 或 `"kWh"`）、`quality`（`[0,1]` 内有限数）及非负整数 `age_minutes`。

未知引用、重复或缺失的 ID/遥测组合、错误类型、缺失键、非法枚举、布尔值代替数值、非有限数与越界值均须拒收。所有拒收必须以非零状态退出，标准错误以 `INPUT_ERROR:` 开头，且已存在输出文件的字节、同目录文件清单均不得改变。

## 单元时段功率证据

单元 `a` 的可用能量为：

```text
usable_energy_kwh = capacity_kwh × (soc_fraction - min_soc_fraction)
nameplate_limited_kw = max_discharge_kw × availability_fraction
```

每条遥测先把 `discharge_energy` 转为 kWh，再按**该审计时段**的时长计算：

```text
observed_kw = discharge_energy_kwh × 60 / interval_duration_minutes
```

遥测仅在同时满足 `quality >= 0.90` 且 `age_minutes <= max_telemetry_age_minutes` 时可信。可信时，单元该时段的 `telemetry_limited_kw` 为 `observed_kw`，`power_cap_kw` 为它与 `nameplate_limited_kw` 的较小值；不可信时 `telemetry_limited_kw` 为 `null`，`power_cap_kw` 即 `nameplate_limited_kw`。

凡存在不可信遥测的单元，或存在可信遥测但 `observed_kw < 0.70 × nameplate_limited_kw` 的单元，均加入 `review_assets`。复核不等同于把单元从容量中删除。

## 可持续功率与 N-1

对一个给定馈线及一个给定失效情景，设 `P` 为该馈线拟承诺的恒定备用功率。只有在存在一组非负调度量 `dispatch[a,t]` 时，`P` 才可持续：

```text
对每个时段 t：所有仍可用单元的 dispatch[a,t] 之和 >= P
dispatch[a,t] <= 该单元在 t 的 power_cap_kw
对每个单元 a：所有时段 dispatch[a,t] × duration_minutes[t] / 60 <= usable_energy_kwh[a]
```

该馈线的 `normal_sustainable_kw` 是没有单元失效时可持续的最大 `P`。

- 若 `n_minus_one` 为假，`contingency_sustainable_kw` 等于 `normal_sustainable_kw`，`contingency_asset_id` 为 `null`。
- 若 `n_minus_one` 为真，必须分别假设该馈线的每一个单元永久失效，并计算剩余单元的最大可持续 `P`。`contingency_sustainable_kw` 是这些值的最小值，`contingency_asset_id` 是导致最小值的单元 ID；并列时取字典序最小 ID。没有单元的馈线两种可持续功率均为零，`contingency_asset_id` 为 `null`。

`firm_kw = min(export_limit_kw, contingency_sustainable_kw)`。每条馈线都必须用自身的 `firm_kw` 与 `required_reserve_kw` 判断 `meets_requirement`。车队 `fleet_firm_kw` 是全部馈线 `firm_kw` 的和；不得把能量或功率跨馈线转移。

最终状态优先级：若总请求未满足，或任一馈线不满足自身需求，则为 `"INSUFFICIENT"`；否则若 `review_assets` 非空则为 `"REVIEW"`；否则为 `"READY"`。

## 成功报告

成功时以 UTF-8 JSON 原子替换输出文件。顶层字段必须为：

```text
status, max_telemetry_age_minutes, reserve_request_kw, fleet_firm_kw,
assets, feeders, review_assets
```

单元报告按 `id` 排序，含 `id`、`feeder_id`、`usable_energy_kwh`、`nameplate_limited_kw` 和按时段 ID 排序的 `intervals`；每个时段项含 `id`、`telemetry_limited_kw`、`power_cap_kw`。馈线报告按 `id` 排序，含 `id`、`normal_sustainable_kw`、`contingency_sustainable_kw`、`contingency_asset_id`、`firm_kw`、`required_reserve_kw`、`meets_requirement`。`review_assets` 按字典序排序。
