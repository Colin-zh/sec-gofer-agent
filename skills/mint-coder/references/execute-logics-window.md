---
id: execute-logics-window
title: Time Window Logic mint.execute Pattern
resource_type: pattern_source
summary: Reusable time-window logic for recent-window filtering, baseline-before-monitoring-window checks, component extraction, time bucketing, and adjacent time-delta calculations. Use this when you need a mask, a value sequence, or a grouped time series; not a single scalar summary. Core primitives are latest, soon, soonest, timestamp_trans, timestamp_aggr, and timestamp_delta. Confirm the time column, unit and offset, period length, and any threshold or grouping requirement.
tags: [execute, logics, window, time-window, latest, soon, soonest, timestamp, time-delta, event_time, recent-window, time-bucketing]
related_source: [execute-logics-session, execute-logics-series, execute-metrics-time, execute-metrics-distribution, execute-metrics-extremum]
priority: 60
---

## 适用场景
适用于“时间维度的窗口/提取/间隔”处理：按时间范围筛选、从时间戳提取分量、按时间分量聚合、以及计算相邻记录间隔。它不是单值指标，也不是图/关系模式。

典型场景：
- 最近 N 天/最近 M 小时内哪些记录命中。
- 监测期之前某个基线窗口中的记录数、金额或其他统计。
- 按小时、天、工作日等提取时间分量并进行分桶。
- 相邻两条记录的时间间隔是否过短或过长。

> 边界：如果你需要“时间跨度 / 速率”这类单值统计，请转到 `execute-metrics-time`；如果需要“相邻值差/涨跌”则转到 `execute-logics-series`；如果需要“先切会话再汇总”则转到 `execute-logics-session`。本模式输出的是时间掩码、时间分量序列、时间差序列或时间分桶结果，不是单值聚合。
> 与 metrics 的耦合：`latest` / `soon` / `soonest` 生成的布尔掩码可以作为 `execute-metrics-distribution` 中真值类统计的输入；时间桶聚合也可以作为时间分布型指标的基础。
> 截止日、自然日/经过时间、时区、基线边界和后续观察完整性见 [时间语义](time-semantics.md)。下面的末次事件锚点只是示例口径，不能自动替代用户指定的评估日。

## 逻辑速查表（话术 → 时间形态 → pandas 写法）

> `t = pd.to_datetime(df[event_time_col], errors="raise")`；先按契约检查缺失时间并统一时区。优先使用 datetime/Timedelta 运算，避免整数秒转换丢失亚秒精度或误把不同 datetime 分辨率当纳秒。以下掩码可用于计数/占比等后续统计。

### A. 最近 N 天（latest）
| 话术 | 时间形态 | pandas 写法 |
|---|---|---|
| 最近 N 个自然日内的记录（末次事件锚点） | 近期掩码 RecentMask | `t >= t.max().normalize() - pd.DateOffset(days=offset-1)` |

> 口径：以 `max`（最新日期）为末端，向前取 `offset-1` 天，并将起点规范到当天 00:00：`.normalize()`。这可以得到与最近 N 天相对应的布尔掩码。

### B. 监测期前 N 天（soon，基线）
| 话术 | 时间形态 | pandas 写法 |
|---|---|---|
| 监测期之前的基线窗口（相对：前 N 个时间桶） | 基线掩码 SoonMask | 按时间桶去重并取前 N 个桶的记录 |
| 监测期之前的基线窗口（绝对：最近 period 个自然日为监测期，紧前 N 个自然日为基线） | 基线掩码 SoonAbsMask | `(t >= baseline_start) & (t < monitor_start)` |

> 用末次事件所在自然日作示例锚点时，`monitor_start = t.max().normalize() - pd.DateOffset(days=period-1)`，`baseline_start = monitor_start - pd.DateOffset(days=N)`；period、N须为正整数。基线必须同时限制上下界。`soon` 的相对口径（前N个时间桶）和绝对口径（监测期之前N个自然日）须确认，不能默认替用户选择。

### C. 不在最近 N 天（soonest）
| 话术 | 时间形态 | pandas 写法 |
|---|---|---|
| 最近窗口之前的有效历史记录 | 历史掩码 PreRecentMask | `t.notna() & (t < recent_start)` |

> 仅当时间全有效、锚点为末次事件且无未来记录时，历史掩码才等于 `~RecentMask`；显式截止日场景中取反还会纳入未来记录。

### D. 时间提取（timestamp_trans）
| 话术 | 时间形态 | pandas 写法 |
|---|---|---|
| 记录发生在第几天/第几周/几点 | 分量序列 | `t.dt.day` / `t.dt.weekday` / `t.dt.hour` |
| 小时段分类（例如凌晨/白天/夜间） | 分段序列 | 按 `t.dt.hour` 分桶 |
| 是否工作日 | 布尔序列 | `t.dt.weekday < 5` |

> 时间提取支持 `day` / `hour` / `minute` / `weekday` 等分量。`weekday < 5` 仅表示周一至周五，不处理节假日/调休；真实工作日需日历资料。

### E. 时间桶（timestamp_aggr）
| 话术 | 时间形态 | pandas 写法 |
|---|---|---|
| 每个时间分量内的记录数/金额 | 时间桶聚合 | `df.groupby(t.dt.hour)[value_col].sum()` 或 `.size()` |
| 时间分量的频数分布 | 分量频数 | `t.dt.hour.value_counts()` |

> 这是“按时间分量 groupby”的聚合。频数分布/熵类指标可转到 `execute-metrics-distribution`，本模式保留的是“按时间分量分桶”的动作本身。

### F. 相邻时间差（timestamp_delta）
| 话术 | 时间形态 | pandas 写法 |
|---|---|---|
| 相邻两条记录间隔多久 | 间隔序列 DeltaSeries | `t.diff().dt.total_seconds() / UNIT_SECONDS[unit]` |
| 间隔是否落在某个区间中 | 间隔分箱掩码 | `(delta >= lo) & (delta < hi)` |

> `UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}` 用于经过时间单位，不代表当地自然日边界。时间差序列可交给 distribution / extremum；例如 `delta.max()` 获取最大间隔。

## 关系解读与借鉴值
> 这些形态输出的是掩码、分量列或时间差序列，必须转回具体业务语义。下表提供的是经验解释，不是硬性标准。

| 时间形态 | 数值含义 | 借鉴值 |
|---|---|---|
| 近期掩码 RecentMask | 最近 N 天内的记录 | 近期活跃度/近期异常密度 |
| 基线掩码 SoonMask | 监测期前的基线窗口 | 对照期与监测期相比，可识别明显增长/下降 |
| 历史掩码 PreRecentMask | 排除近期之后的历史记录 | 用于比较最近行为与历史行为 |
| 分量序列（hour/weekday） | 在几点/周几发生 | 例如固定时段集中或深夜行为 |
| 时间桶聚合 | 每个时间分量的聚合结果 | 找到异常时间段或高峰时间 |
| 间隔序列 DeltaSeries | 相邻记录之间的时间差 | 极短间隔通常提示重复/密集/快速连续事件 |

## 参数（口径需先确认）
- `event_time_col`: 时间字段，通常 `event_time`。
- `unit` / `offset`: latest / soonest 的时间单位和跨度（例如天/周/月，offset >= 1）。
- `period` / `days` / `N`: soon 的监测期长度和基线长度；确认相对还是绝对口径。
- `section`: cutoff 类回溯天数（见 session 文档）。
- `gap`: 间隔阈值（秒，见 session 文档的 gap 划窗）。
- `value_col`: 时间桶聚合的数值字段，可选。
- `partition_cols`: 分组键，通常 `[date_key, entity_id]`；latest / soon / soonest 需要按实体执行。
- `sort_col` / `order`: `diff` 依赖时间升序，需通过 `partition` 传入 `presort`。

## 示例

### 示例一：最近 N 天 + 不在最近 N 天
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 5,
    "entity_id": ["e1"] * 5,
    "event_time": ["2023-01-01", "2023-01-03", "2023-01-06", "2023-01-09", "2023-01-12"],
    "value": [100, 200, 150, 300, 250],
})

def recent_window(
    df: pd.DataFrame, partition_cols: List[str], event_time_col: str, value_col: str, offset: int
) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
        raise ValueError("offset must be a positive integer")
    t = pd.to_datetime(df[event_time_col], errors="raise")
    if t.isna().any():
        raise ValueError("event time must not be missing")
    start = t.max().normalize() - pd.DateOffset(days=offset - 1)
    recent = t >= start
    pre_recent = ~recent
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "recent_values": "|".join(str(v) for v in df.loc[recent, value_col]),
        "recent_count": int(recent.sum()),
        "pre_recent_count": int(pre_recent.sum()),
    }]

result = execute(
    df,
    recent_window,
    schema="date_key:str,entity_id:str,recent_values:str,recent_count:long,pre_recent_count:long",
    params=dict(partition_cols=["date_key", "entity_id"], event_time_col="event_time", value_col="value", offset=5),
    partition={"by": ["date_key", "entity_id"]},
)
print(result.to_markdown())
```

说明：
- `offset=5` → 窗口起点为 01-08 00:00；近期值为 {01-09(300), 01-12(250)}，`recent_count=2`；历史值为 {01-01,01-03,01-06}，`pre_recent_count=3`。
- `recent` 和 `pre_recent` 都是布尔掩码；如果需要计数/占比，请转到 `execute-metrics-distribution` 的真值类形态。
- 本例时间完整且以末次事件为锚点，所以`~recent`表示窗口前历史；指定截止日时需排除未来记录。

### 示例二：时间提取 + 时间桶
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 6,
    "entity_id": ["e1"] * 6,
    "event_time": ["2023-01-01 09:30:00", "2023-01-01 14:00:00", "2023-01-02 10:00:00",
                   "2023-01-02 22:30:00", "2023-01-03 02:00:00", "2023-01-03 23:00:00"],
    "value": [100, 200, 150, 300, 50, 250],
})

def hour_bucket(
    df: pd.DataFrame, partition_cols: List[str], event_time_col: str, value_col: str
) -> List[Dict[str, Any]]:
    t = pd.to_datetime(df[event_time_col])
    hour = t.dt.hour
    def hour_range(h):
        return "night" if h < 8 else ("day" if h < 20 else "evening")
    night = hour.apply(hour_range).eq("night")
    hourly_value = df.assign(h=hour).groupby("h")[value_col].sum()
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "hour_list": "|".join(str(h) for h in hour),
        "night_count": int(night.sum()),
        "hourly_value_map": "|".join(f"{k}:{v}" for k, v in hourly_value.items()),
    }]

result = execute(
    df,
    hour_bucket,
    schema="date_key:str,entity_id:str,hour_list:str,night_count:long,hourly_value_map:str",
    params=dict(partition_cols=["date_key", "entity_id"], event_time_col="event_time", value_col="value"),
    partition={"by": ["date_key", "entity_id"]},
)
print(result.to_markdown())
```

说明：
- `t.dt.hour` 提取小时分量，`hour_list="9|14|10|22|2|23"`。
- `hour_range` 将小时分成 `night/day/evening` 三个段；`night_count=1` 表示凌晨 2 点有一条记录。
- `groupby(h).sum()` 按小时分桶聚合数值；如果需要找到最繁忙的时间段或分布峰值，可进一步转到 `execute-metrics-distribution`。

### 示例三：相邻时间差（shape-preserving 加列）
```python
import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 5,
    "entity_id": ["e1"] * 5,
    "event_time": ["2023-01-01 10:00:00", "2023-01-01 10:15:00", "2023-01-01 18:00:00",
                   "2023-01-03 10:00:00", "2023-01-03 10:00:30"],
    "value": [100, 200, 150, 300, 250],
})

def add_time_delta(df: pd.DataFrame, event_time_col: str) -> pd.DataFrame:
    t = pd.to_datetime(df[event_time_col], errors="raise")
    if t.isna().any():
        raise ValueError("event time must not be missing")
    out = df.copy()
    out["delta_min"] = t.diff().dt.total_seconds() / 60.0
    return out

result = execute(
    df,
    add_time_delta,
    schema="*,delta_min:double",
    params=dict(event_time_col="event_time"),
    partition={"by": ["date_key", "entity_id"], "presort": "event_time asc"},
)
print(result.to_markdown())
```

说明：
- `t.diff().dt.total_seconds() / 60` 得到相邻记录间的分钟差并保留亚秒精度；首项为 NaN。
- 最后两条记录间隔 30 秒，`delta_min = 0.5`，表明是非常紧密的连续事件。
- 如果需要单值摘要（最大间隔、间隔小于 1 分钟的记录数），请转到 `execute-metrics-extremum` 或 `execute-metrics-distribution`。

## 生成注意事项
- **保留时间精度**：优先使用datetime/Timedelta；显式要求整数秒时才转换epoch，并验证分辨率与缺失值。
- **latest / soonest 是布尔掩码**：确认末次事件还是显式评估日锚点；显式截止自然日使用含当天的半开时间戳区间，历史掩码不包含未来或缺失时间。
- **soon 的两种口径必须确认**：相对口径（前N个时间桶）与绝对口径（监测期之前N个自然日）；基线同时设置上下界。
- **diff 依赖排序**：相邻差/时间差都要求按时间升序，`presort` 必须设置。
- **shape-preserving 新增列要声明输出列**：`schema="*"` 只保留输入中已有的列，不包含计算函数添加的列；使用 `schema="*,new_col:type"`，或声明完整输出 schema。仅未新增列且输出列与输入完全一致时才使用 `schema="*"`。布尔掩码/聚合值使用 `sum`/`mean` 或 string join；单值输出需显式 schema。
- 函数中不要写死字段名：用 `df[event_time_col]`、`df[value_col]` 由 `params` 注入；分组键提交为 `partition_cols`。
- 不要杜撰 API；本模式只依赖 pandas 标准方法：`to_datetime`、`dt.hour`、`dt.day`、`dt.weekday`、`diff`、`groupby`、`normalize`、`Timedelta` 等。
