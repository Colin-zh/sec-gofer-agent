---
id: execute-metrics-location
title: Location Metrics mint.execute Pattern
resource_type: pattern_source
summary: Relative position of a series maximum or minimum, reporting the first or last occurrence on an ordinal scale from 0 (first observation) to 1 (last observation). This is record-order position, not elapsed time; it requires presort when order is defined by time and is not comparable across sequences with different lengths or irregular timing. Use execute-metrics-time for elapsed-time distances. Confirm value_col, partition_cols, and ordering.
tags: [execute, metrics, location, position, max-location, min-location, 位置, 峰值位置, 谷值位置, 首次, 末次]
related_source: [execute-metrics-extremum, execute-metrics-distribution, execute-metrics-time]
priority: 70
---

## 指标范围
适用于定位数值序列中最大值或最小值的首次/末次出现位置。极值本身及其占比见 `execute-metrics-extremum` 和 `execute-metrics-distribution`；实际经过时间见 `execute-metrics-time`。

## 指标速查表（分析目标 → 指标 → pandas 写法）

> 下表中 `s = df[value_col].to_numpy()`（任意待分析的连续列转 numpy 数组），`n = len(s)`。
> **位置比例 = 该值在序列中的零起始位置 / (n-1)**，`0` = 第一条记录、`1` = 最后一条记录（需 `presort` 按时间排序后才有此含义）。
> ⚠️ 这是**记录序号位置**，不是**时间位置**：它只描述序列中的序号。序列长度不同或时间间隔不均时，不可直接作跨序列或经过时间比较；实际时间差见 `execute-metrics-time`。

| 分析目标 | 指标 | pandas 写法 |
|---|---|---|
| 峰值出现在序列哪个位置（首次） | 首次最大值位置 FirstMaxLocation | `first = int(s.argmax()); first / (n - 1)` |
| 峰值最后一次出现在哪个位置 | 末次最大值位置 LatestMaxLocation | `last = int(n - 1 - s[::-1].argmax()); last / (n - 1)` |
| 谷值出现在序列哪个位置（首次） | 首次最小值位置 FirstMinLocation | `first = int(s.argmin()); first / (n - 1)` |
| 谷值最后一次出现在哪个位置 | 末次最小值位置 LatestMinLocation | `last = int(n - 1 - s[::-1].argmin()); last / (n - 1)` |

> `s.argmax()` / `s.argmin()` 返回**零起始位置**（首次出现的序号），与 index 标签无关；末次位置用「倒序 argmax 再换算」：`n - 1 - s[::-1].argmax()`。
> 位置占比统一拆成「首次（First）」与「末次（Latest）」两个明确口径，避免用含糊的「位置」封装。
> 长度 `n == 1` 时没有可比较的位置跨度；示例使用安全分母 1，因此位置比例为 0。

## 指标解释

| 指标 | 数值含义 | 解释 |
|---|---|---|
| 首次最大值位置 FirstMaxLocation | 最大值首次出现的序列阶段 | 越接近 0 越靠近序列开头，越接近 1 越靠近序列末尾；数值表示序号比例，不代表时间比例 |
| 末次最大值位置 LatestMaxLocation | 最大值最后一次出现的位置 | 与首次位置对比可识别并列最大值分布在序列中的范围；极值计数可与 `execute-metrics-extremum` 的 MaxCount 对照 |

> 以上是**记录序号位置**，不是经过时间。计算峰值距末次观测或参考时点的时长时，使用 `execute-metrics-time` 的时间差定义。

## 参数
- `value_col`: 数值字段名；示例使用 `value`。
- `partition_cols`: 分组字段，由数据集结构和分析粒度决定。
- `sort_col` / `order`: 位置比例依赖的排序字段与方向；示例按 `event_time`、`event_clock` 升序，通过 `partition` 的 `presort` 传入，**不是函数参数**。

## 示例

### 示例一：峰值/谷值位置占比（首次 / 末次）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 4 + ["2023-01-02"] * 4,
    "group_id": ["group_a"] * 4 + ["group_b"] * 4,
    "event_time": ["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04",
                  "2023-01-05", "2023-01-06", "2023-01-07", "2023-01-08"],
    "event_clock": ["09:00"] * 8,
    "value": [100, 30, 20, 10, 8, 5, 6, 50],
})

def peak_location(df: pd.DataFrame, partition_cols: List[str], value_col: str) -> List[Dict[str, Any]]:
    s = df[value_col].to_numpy()
    n = len(s)
    denom = n - 1 if n > 1 else 1  # 单记录序号列退化，避免除 0
    first_max = int(s.argmax())
    latest_max = int(n - 1 - s[::-1].argmax())
    first_min = int(s.argmin())
    latest_min = int(n - 1 - s[::-1].argmin())
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "first_max_loc": first_max / denom,
        "latest_max_loc": latest_max / denom,
        "first_min_loc": first_min / denom,
        "latest_min_loc": latest_min / denom,
    }]

result = execute(
    df,
    peak_location,
    schema="group_date:str,group_id:str,first_max_loc:double,latest_max_loc:double,first_min_loc:double,latest_min_loc:double",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value"),
    partition={"by": ["group_date", "group_id"], "presort": "event_time asc, event_clock asc"},
)
print(result.to_markdown())
```

说明：
- `presort: "event_time asc, event_clock asc"` 让函数拿到「分组内按时间升序」的数据，此时 `argmax()` 的下标才对应「最早/最近」；不排序则「位置」只是物理顺序，无业务含义。
- group_a 的峰值 100 在最早一记录（`first_max_loc=0`），谷值 10 在最后一记录（`first_min_loc=1`）；group_b 的峰值 50 在最后一记录（`first_max_loc=1`），谷值 5 在最早一记录（`first_min_loc=0`）。
- 用 `s.to_numpy().argmax()` 而非 `s.idxmax()`，是为了拿到**记录序号**而非 index 标签，规避非连续索引带来的偏差。
- 这些是「记录序号位置」；经过时间差的计算方式见 `execute-metrics-time`。

## 实现注意事项
- **位置比例必须配合 `partition` + `presort` 按时间排序**，否则「位置」无业务含义；排序字段优先 `event_time`，同日多记录再加 `event_clock`。
- 位置比例采用 `position/(n-1)`（`0`=首条、`1`=末条）；它不表示经过时间。若采用 `(index+1)/n` 等替代定义，应明确标注，因为端点和单记录行为不同。长度 `n == 1` 时示例分母取 1，返回位置 0；空序列需单独处理。
- 「均值上下的计数/求和」不归本模式——它本质是「满足某条件」的真值类，`cond = s > s.mean()`，见 `execute-metrics-distribution` 的真值四口径。
- `argmax()` / `argmin()` 对并列极值返回首次出现的位置；反向搜索得到末次出现位置。缺失值应在计算前按分析定义处理。
- 聚合/统计结果结构通常与输入不同，**必须写显式 schema**，不要用 `schema="*"`。
- 结果统一用 `result = execute(...)` 命名，业务参数经 `params=dict(...)` 传入（`value_col` / `partition_cols` 等）。
- 函数内**不要写死列名**：一律用 `df[value_col]`，具体字段名经 `params=dict(...)` 注入；分组键用 `partition_cols: List[str]` 接收，返回里用 `**{col: df.iloc[0][col] for col in partition_cols}` 透传。
- 不要杜撰不存在的 API；本模式涉及的 numpy/pandas 方法（`argmax` / `argmin` / 切片 `[::-1]`）均为标准方法。
