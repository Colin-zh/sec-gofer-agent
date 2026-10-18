---
id: execute-metrics-extremum
title: Extremum Metrics mint.execute Pattern
resource_type: pattern_source
summary: Extremum metrics for numeric series: maximum, minimum, range, counts of maximum/minimum values, and the final value after ordering. The final value is order-dependent and requires presort when it represents the latest observation. Range alone does not establish an outlier; compare with distribution metrics where useful. Confirm value_col, partition_cols, and ordering.
tags: [execute, metrics, extremum, max, min, ptp, range, last-value, 最大值, 最小值, 极值, 峰值, 谷值, 末值, 极差]
related_source: [execute-metrics-distribution, execute-metrics-location, execute-metrics-time]
priority: 80
---

## 指标范围
适用于 numeric Series 的最大值、最小值、极差、极值重复次数与排序后的末值。按类别或实体汇总排名属于 distribution/concentration 指标（见 `execute-metrics-distribution`）。

## 指标速查表（分析目标 → 指标 → pandas 写法）

| 分析目标 | 指标 | pandas 写法 |
|---|---|---|
| 最大记录值 | 最大值 Max | `s.max()` |
| 最小记录值 | 最小值 Min | `s.min()` |
| 最大与最小差距多大 | 极差 Ptp | `s.max() - s.min()` |
| 峰值数值出现几次 | 最大值出现次数 MaxCount | `(s == s.max()).sum()` |
| 谷值数值出现几次 | 最小值出现次数 MinCount | `(s == s.min()).sum()` |
| 单条最大 / 最小值占数值总和的比例 | MaxRatio / MinRatio | `s.max() / s.sum()` / `s.min() / s.sum()`，最大值按一次计，不乘出现次数 |
| 所有等于最大值的记录值合计占数值总和的比例 | MaxTotalRatio | `s.max() * (s == s.max()).sum() / s.sum()`；MaxRatio 只看单条最大值，MaxTotalRatio 把并列最大值全部计入 |
| 所有等于最小值的记录值合计占数值总和的比例 | MinTotalRatio | `s.min() * (s == s.min()).sum() / s.sum()`；MinRatio 只看单条最小值，MinTotalRatio 把并列最小值全部计入 |
| 最近一记录 / 最新值是多少 | 末值 LastValue | `s.iloc[-1]`（需 `presort` 按时间排序） |

> 上表中 `s` 表示「任意待分析的数值列 Series」，具体列名经参数传入（如数值列 `df[value_col]`，也可为其它连续字段）。

## 指标解释

| 指标 | 数值含义 | 解释 |
|---|---|---|
| 极差 Ptp | 最大值与最小值之差 | 对单个极端值敏感；不区分整体波动与单个离群点。可与 IQR 等分布统计量比较，但没有通用判定阈值 |
| 最大值出现次数 MaxCount | 最大值在有效数据中的重复次数 | 大于 1 表示重复出现；若需计入所有并列最大值的总和比例，使用 distribution 的 MaxTotalRatio |
| 最小值出现次数 MinCount | 最小值在有效数据中的重复次数 | 大于 1 表示重复出现；若需计入所有并列最小值的总和比例，使用 distribution 的 MinTotalRatio |
| 末值 LastValue | 依赖排序 | 要解释为最新观测，必须配合 `presort` 按时间排序；不排序时它只是物理顺序的最后一行 |

## 参数
- `value_col`: 数值字段名；示例使用 `value`。
- `partition_cols`: 分组字段，由数据集结构和分析粒度决定。
- `sort_col` / `order`: 末值依赖的排序字段与方向；示例按 `event_time` 升序，通过 `partition` 的 `presort` 传入，**不是函数参数**。

## 示例

### 示例一：基础极值（最大 / 最小 / 极差 / 峰值谷值出现次数）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 4 + ["2023-01-02"] * 3,
    "group_id": ["group_a"] * 4 + ["group_b"] * 3,
    "value": [100, 50, 100, 10, 8, 8, 20],
})

def extremum_metrics(df: pd.DataFrame, partition_cols: List[str], value_col: str) -> List[Dict[str, Any]]:
    s = df[value_col]
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "max_value": float(s.max()),
        "min_value": float(s.min()),
        "ptp": float(s.max() - s.min()),
        "max_count": int((s == s.max()).sum()),
        "min_count": int((s == s.min()).sum()),
    }]

result = execute(
    df,
    extremum_metrics,
    schema="group_date:str,group_id:str,max_value:double,min_value:double,ptp:double,max_count:long,min_count:long",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value"),
    partition={"by": ["group_date", "group_id"]},
)
print(result.to_markdown())
```

说明：
- `max_count` / `min_count` 统计峰值/谷值数值出现了几次；大于 1 说明该数值重复出现（group_a 的 100 出现两次）。
- 列名不写死：分组键用 `partition_cols` + `**{col: df.iloc[0][col] for col in partition_cols}` 透传，指标列用 `df[value_col]`，真实字段名经 `params=dict(...)` 注入。
- `ptp` 大只说明「最大减最小」大，不代表一定有异常；要判断尾部极端值，另算 Ptp/Iqr 比值。

### 示例二：末值（最近一记录事件数值，配合 presort 按时间排序）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 3 + ["2023-01-02"] * 2,
    "group_id": ["group_a", "group_a", "group_a", "group_b", "group_b"],
    "event_time": ["2023-01-01", "2023-01-03", "2023-01-02", "2023-01-05", "2023-01-04"],
    "value": [100, 300, 200, 50, 90],
})

def latest_value(df: pd.DataFrame, partition_cols: List[str], value_col: str) -> List[Dict[str, Any]]:
    s = df[value_col]
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "latest_value": float(s.iloc[-1]),
    }]

result = execute(
    df,
    latest_value,
    schema="group_date:str,group_id:str,latest_value:double",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value"),
    partition={"by": ["group_date", "group_id"], "presort": "event_time asc"},
)
print(result.to_markdown())
```

说明：
- `presort: "event_time asc"` 让函数拿到「分组内按日期升序」的数据，`s.iloc[-1]` 即最近一记录。
- 若需精确到同一天内的先后，再加 `event_clock`（事件时间）到排序字段：`presort: "event_time asc, event_clock asc"`。
- 不排序就用 `iloc[-1]` 是错的——那只是物理顺序最后一行，无业务含义。

## 实现注意事项
- 聚合/统计结果结构通常与输入不同，**必须写显式 schema**，不要用 `schema="*"`。
- 结果统一用 `result = execute(...)` 命名，业务参数经 `params=dict(...)` 传入（`value_col` / `partition_cols` 等）。
- 函数内**不要写死列名**：一律用 `df[value_col]`，具体字段名经 `params=dict(...)` 注入；分组键用 `partition_cols: List[str]` 接收，返回里用 `**{col: df.iloc[0][col] for col in partition_cols}` 透传。
- 末值 `s.iloc[-1]` 必须配合 `partition` + `presort` 按时间排序，否则无业务含义；排序字段优先 `event_time`，同日多记录再加 `event_clock`。
- `ptp` 是范围统计，不是异常判定；必要时结合 IQR、分位数或适合该数据的离群规则分析。
- `max_count` / `min_count` 大于 1 表示极值重复出现；重复值本身不能证明存在特定模式。
- 极值出现次数忽略比较结果为缺失的记录；所有值均缺失或序列为空时，应先定义缺失数据处理方式。MaxRatio / MinRatio 及其合计比例要求分母 `s.sum()` 非零。
- 比例公式仅在数值总和适合作为分母时具有通常的占比解释；含正负值时结果可能为负或超出 0~1。
- 不要杜撰不存在的 API；本模式涉及的 pandas 方法（`max` / `min` / `iloc` / 布尔比较 `.sum()`）均为标准方法。
