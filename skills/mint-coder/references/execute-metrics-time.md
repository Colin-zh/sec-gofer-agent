---
id: execute-metrics-time
title: Time Metrics mint.execute Pattern
resource_type: pattern_source
summary: Time-based metrics for event records and time series: elapsed span, count/value/entity rates, inter-event gaps, and elapsed time from an extremum to the last observation. Convert spans and gaps to a consistent unit. Rate denominators distinguish total elapsed span from active-date count; specify time_col, value_col, entity_id_col, partition_cols, unit, and denominator definition.
tags: [execute, metrics, time, range, velocity, 时间跨度, 速率, 持续时间, 平均每天, 有效事件日, 时间位置, 事件间隔, 实体速率]
related_source: [execute-logics-window, execute-metrics-location, execute-metrics-trend]
priority: 70
---

## 指标范围
适用于事件记录的整体时间跨度、单位时间速率、相邻事件间隔，以及某个数值极值相对末次观测的时间差。窗口筛选和时间桶分布是独立计算步骤（见 `execute-logics-window`）；本参考中的指标通常产出每分区单个标量。

## 指标速查表（分析目标 → 指标 → pandas 写法）

> `ts = pd.to_datetime(df[time_col])` 先把时间列转成 datetime；`span_days` 为跨度转成天数。

| 分析目标 | 指标 | pandas 写法 |
|---|---|---|
| 事件持续多久 | 时间跨度 TimeRange | `span = (ts.max() - ts.min())`（Timedelta，`.days` / `.total_seconds()/3600` 转单位） |
| 平均每天几记录 | 速率（记录数） VelocityCount | `df.shape[0] / span_days` |
| 平均每天数值总和 | 速率（数值） VelocityValue | `df[value_col].sum() / span_days` |
| 平均每天涉及几个实体 | 速率（实体数） VelocityEntity | `df[entity_id_col].nunique() / span_days` |
| 每个有事件日期平均几条记录 | 有效事件日速率（记录数） ActiveDayCount | `df.shape[0] / df[time_col].nunique()` |
| 每个有事件日期的平均数值总和 | 有效事件日速率（数值） ActiveDayValue | `df[value_col].sum() / df[time_col].nunique()` |
| 每个有事件的日子平均几个实体 | 有效事件日速率（实体数） ActiveDayEntity | `df[entity_id_col].nunique() / df[time_col].nunique()` |
| 最大一记录距最近一记录多久 | 峰值距末次时间差 PeakToLast | `(ts.max() - ts[df[value_col].idxmax()]).days`（0 = 峰值就是最近一记录） |
| 相邻记录的平均间隔 | 平均事件间隔 MeanGap | `ts.diff().dt.days.mean()`（需 presort，首项 NaN） |
| 相邻记录的最大间隔 | 最大事件间隔 MaxGap | `ts.diff().max()`（Timedelta → `.days`） |
| 间隔离散程度 | 间隔标准差 GapStd | `ts.diff().dt.days.std()` |

> `span_days = span.days`，当跨度不足完整一天时，下面示例采用显式约定的1天最小分母，输出的原跨度仍为0；这是演示策略，不是通用默认。真实需求需选择精确经过时间、已知观察期或未知值，不能仅为避免除零改变分母。精确跨度使用总秒数换算并统一输出单位，参见 [时间语义](time-semantics.md)。
> 速率有**两个分母口径 × 三个维度（记录数/数值/实体数）**：`VelocityCount/VelocityValue/VelocityEntity` 用时间跨度 `max - min`（`span_days`，包含无事件间隔）；`ActiveDayCount/ActiveDayValue/ActiveDayEntity` 用有效日期数 `df[time_col].nunique()`（只计有事件的日期）。只有当 `time_col` 是按日归一化的日期字段时，`nunique()` 才表示有效天数；时间戳字段应先提供或构造日期粒度字段。不同分母定义会产生不同统计口径。
> 「峰值距末次时间差」是「峰值是早还是晚」的**时间位置**表达，替代 `execute-metrics-location` 的「记录序号位置」；记录数不均衡/时间非等间隔时，时间差比记录序号占比更可靠。若业务要的是「距今天数」而非「距末次天数」，把 `ts.max()` 换成参考日（监测点/当前日期，经参数传入）。
> 事件间隔 `ts.diff()` 是相邻两记录的时间差序列（首项 NaN，自动忽略），**需 `presort` 按时间排序**；`.dt.days` 转天、精确到小时用 `.dt.total_seconds()/3600`。间隔指标衡量「事件节奏」，与 `execute-metrics-trend` 的 `s.diff()`（数值差）同构但作用在时间列上。

## 指标解释

| 指标 | 数值含义 | 解释 |
|---|---|---|
| 时间跨度 TimeRange | 首次→末次事件间隔 | 与记录数结合可描述观测频率；跨度长且记录少表示观测较稀疏。跨组比较需统一单位和观测范围 |
| 速率（记录数） VelocityCount | 整个观察窗口内平均每天记录数（分母=时间跨度，非有效事件天数） | 反映整个观察窗口的记录频率；跨度分母包含无事件间隔，与有效日期口径不同 |
| 速率（数值） VelocityValue | 单位时间内数值总和 | 结合跨度与记录数描述单位时间累计值和记录频次 |
| 速率（实体数） VelocityEntity | 整个分区内的不同实体数除以时间跨度 | 分子是整个分区的 distinct entity count，不是逐日实体数之和；可与记录数速率联合描述规模 |
| 有效事件日速率（记录数） ActiveDayCount | 每个有事件日期平均记录数（分母=有效日期数 nunique） | 排除无事件日期，反映活跃日期的平均记录数；与 VelocityCount 使用不同的时间分母 |
| 有效事件日速率（数值） ActiveDayValue | 每个有事件日期的平均数值总和 | 反映每个活跃日期的平均累计数值 |
| 有效事件日速率（实体数） ActiveDayEntity | 整个分区内的不同实体数除以有效日期数 | 分子是整个分区的 distinct entity count，不是逐日实体数之和；与 ActiveDayCount 联合描述记录数和实体数 |
| 峰值距末次时间差 PeakToLast | 最大值记录距最后一条记录多少天 | 0 = 最大值记录与最后一条记录时间相同；较大的正值表示最大值记录时间更早。它是经过时间，不同于 execute-metrics-location 的序号位置 |
| 平均事件间隔 MeanGap | 相邻两记录的平均间隔 | 与 VelocityCount 互补：VelocityCount 使用总跨度，MeanGap 基于相邻观测的间隔 |
| 最大事件间隔 MaxGap | 相邻两记录最长的一次间隔 | 描述序列中最长的观测空档 |
| 间隔标准差 GapStd | 相邻间隔的离散程度 | 较大表示间隔变化较大；较小表示间隔较一致。结合 MeanGap 和单位解读 |

## 参数
- `time_col`: 时间字段；示例使用按日期记录的 `event_time`，须可转换为 datetime。
- `value_col`: 数值字段；示例使用 `value`。
- `entity_id_col`: 实体标识字段；用于 distinct entity rates，缺失标识按 `nunique()` 默认规则处理。
- `partition_cols`: 分组字段，由数据集结构和分析粒度决定。
- `unit`: 速率的时间单位（day / hour），决定结果量纲。

## 示例

### 示例一：时间跨度 + 速率 + 有效事件日速率（对应速查表前五行；峰值距末次见示例二）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 4 + ["2023-01-02"] * 3,
    "group_id": ["group_a"] * 4 + ["group_b"] * 3,
    "event_time": ["2023-01-01", "2023-01-05", "2023-01-08", "2023-01-12",
                  "2023-01-01", "2023-01-02", "2023-01-03"],
    "value": [100, 200, 300, 400, 10, 20, 30],
    "entity_id": ["entity_1", "entity_2", "entity_3", "entity_4", "entity_5", "entity_6", "entity_7"],
})

def time_metrics(
    df: pd.DataFrame, partition_cols: List[str], value_col: str, time_col: str, entity_id_col: str
) -> List[Dict[str, Any]]:
    ts = pd.to_datetime(df[time_col])
    span = (ts.max() - ts.min()).days
    span_days = span if span > 0 else 1  # 单日跨度退化，避免除 0
    active_days = df[time_col].nunique()  # 有效事件天数
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "span_days": int(span),
        "velocity_count": float(df.shape[0] / span_days),
        "velocity_value": float(df[value_col].sum() / span_days),
        "velocity_entity": float(df[entity_id_col].nunique() / span_days),
        "active_day_count": float(df.shape[0] / active_days),
        "active_day_value": float(df[value_col].sum() / active_days),
        "active_day_entity": float(df[entity_id_col].nunique() / active_days),
    }]

result = execute(
    df,
    time_metrics,
    schema="group_date:str,group_id:str,span_days:long,velocity_count:double,velocity_value:double,velocity_entity:double,active_day_count:double,active_day_value:double,active_day_entity:double",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value", time_col="event_time", entity_id_col="entity_id"),
    partition={"by": ["group_date", "group_id"]},
)
print(result.to_markdown())
```

说明：
- 时间跨度 = `max - min` 再 `.days` 转天数；group_a 从 01-01 到 01-12 跨度 11 天，group_b 从 01-01 到 01-03 跨度 2 天。
- 速率 = 记录数或数值 / 跨度天数：group_a 每天约 4/11≈0.36 条记录、1000/11≈90.9 数值单位；group_b 每天 3/2=1.5 条记录、60/2=30 数值单位。
- 本例明确采用单日跨度的1天最小分母；其他任务不可未经确认复用。
- 两种分母对照：group_a 跨度 11 天、有效日期 4 天 → velocity_count≈0.36、active_day_count=1.0；group_b 跨度 2 天、有效日期 3 天 → velocity_count=1.5、active_day_count=1.0。两个值描述不同的时间分母，可能高低不同。
- 实体维度同理：group_a 实体 4 个 → velocity_entity≈0.36、active_day_entity=1.0；group_b 实体 3 个 → velocity_entity=1.5、active_day_entity=1.0。
- 列名不写死：分组键用 `partition_cols` + `**{col: df.iloc[0][col] for col in partition_cols}` 透传，指标列用 `df[value_col]` / `df[time_col]`。

### 示例二：峰值距末次时间差（时间位置，替代「记录序号位置」）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 3 + ["2023-01-02"] * 3,
    "group_id": ["group_a"] * 3 + ["group_b"] * 3,
    "event_time": ["2023-01-01", "2023-01-03", "2023-01-05",
                  "2023-01-01", "2023-01-02", "2023-01-03"],
    "value": [100, 50, 30, 10, 20, 30],
})

def peak_to_last(
    df: pd.DataFrame, partition_cols: List[str], value_col: str, time_col: str
) -> List[Dict[str, Any]]:
    ts = pd.to_datetime(df[time_col])
    peak_idx = df[value_col].idxmax()
    peak_to_last = (ts.max() - ts[peak_idx]).days
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "peak_to_last_days": int(peak_to_last),
    }]

result = execute(
    df,
    peak_to_last,
    schema="group_date:str,group_id:str,peak_to_last_days:long",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value", time_col="event_time"),
    partition={"by": ["group_date", "group_id"]},
)
print(result.to_markdown())
```

说明：
- `ts[df[value_col].idxmax()]` 取「数值最大的那记录」的时间，再和末次时间相减，得「峰值距最近一记录多少天」。
- group_a 峰值 100 在 01-01，末次 01-05，`peak_to_last_days=4`（峰值已久远）；group_b 峰值 30 在 01-03 就是末次，`=0`（峰值就是最近一记录）。
- 这是「峰值是早还是晚」的**时间位置**表达，比 `execute-metrics-location` 的「记录序号位置」更可靠（不受记录数不均衡影响）；若要「距今天数」而非「距末次天数」，把 `ts.max()` 换成参考日（监测点/当前日期，经参数传入）。

### 示例三：事件时间间隔（平均间隔 / 最长空窗 / 节奏）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 4 + ["2023-01-02"] * 3,
    "group_id": ["group_a"] * 4 + ["group_b"] * 3,
    "event_time": ["2023-01-01", "2023-01-05", "2023-01-08", "2023-01-12",
                  "2023-01-01", "2023-01-02", "2023-01-03"],
})

def gap_metrics(df: pd.DataFrame, partition_cols: List[str], time_col: str) -> List[Dict[str, Any]]:
    ts = pd.to_datetime(df[time_col])
    gaps = ts.diff().dt.days  # 相邻两记录间隔（首项 NaN）
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "mean_gap_days": float(gaps.mean()),
        "max_gap_days": int(gaps.max()),
        "gap_std": float(gaps.std()),
    }]

result = execute(
    df,
    gap_metrics,
    schema="group_date:str,group_id:str,mean_gap_days:double,max_gap_days:long,gap_std:double",
    params=dict(partition_cols=["group_date", "group_id"], time_col="event_time"),
    partition={"by": ["group_date", "group_id"], "presort": "event_time asc"},
)
print(result.to_markdown())
```

说明：
- `ts.diff().dt.days` 得到相邻两记录的间隔（首项 NaN，聚合时自动忽略）。
- group_a 间隔 `[4, 3, 4]` → mean_gap≈3.67、max_gap=4；group_b 间隔 `[1, 1]` → mean_gap=1、max_gap=1。max_gap 较大表示序列中存在较长的相邻观测间隔。
- 必须 `presort` 按时间排序，否则「相邻」无业务含义；精确到小时用 `ts.diff().dt.total_seconds()/3600`。

## 实现注意事项
- `time_col` 需可转换为 datetime；日期字符串直接 `.days` 会报错。有效日数的分母要求日期粒度，时间戳数据应使用归一化日期或明确的日期字段。
- 时间跨度转统一单位再算速率：`.days`（天）或 `.total_seconds()/3600`（小时），单位要在结果/说明里讲清（记录/天、数值单位/天）。
- 单日跨度分母为0时按口径返回未知，或采用明确约定的观察期/最小分母；不得只为避免除零静默改为1天。
- 速率 = 记录数 / 数值总和 / 不同实体数，再除以指定时间分母；不要混淆速率和累计值。
- 速率有两个分母口径（× 记录数/数值/实体数三维）：`Velocity*` 用时间跨度 `max - min`（包含无事件间隔）；`ActiveDay*` 用有效日期数 `df[time_col].nunique()`（只计有事件的日期）。选择口径时应依据分析定义；二者不可混为一谈。
- 事件间隔 `ts.diff()` 需 `presort` 按时间排序（首项 NaN 自动忽略）；间隔单位与跨度一致（`.dt.days` 或 `.dt.total_seconds()/3600`）。间隔指标看「节奏」，与速率的「密度」是两回事。
- 聚合/统计结果结构通常与输入不同，**必须写显式 schema**。
- 函数内**不要写死列名**：一律用 `df[value_col]` / `df[time_col]` / `df[entity_id_col]`，具体字段名经 `params=dict(...)` 注入；分组键用 `partition_cols: List[str]` 接收，返回里用 `**{col: df.iloc[0][col] for col in partition_cols}` 透传。
- 若只含一条记录，时间跨度为0；本例最小分母是明确演示约定，其他任务须独立确认。间隔序列没有有效差值，MeanGap、MaxGap、GapStd均无定义。空分区按输出契约处理。
- 若峰值有并列，`idxmax()` 选择首个最大值对应的索引，因此 PeakToLast 使用该条记录的时间；若需不同的并列规则，应先明确选择规则。
- `.dt.days` 仅保留整天数，间隔样例也会将不足一天的间隔截为 0 天；需要小数天或更细精度时，使用 `.dt.total_seconds()` 并统一单位。
- 不要杜撰不存在的 API；本模式涉及的 pandas 方法（`pd.to_datetime` / `.max` / `.min` / `.diff` / `.dt.days` / `.nunique` / Timedelta）均为标准方法。
