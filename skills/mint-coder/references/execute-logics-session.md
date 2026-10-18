---
id: execute-logics-session
title: Session / Segmentation Logic mint.execute Pattern
resource_type: pattern_source
summary: Reusable logic for splitting a stream of records into sessions/windows before computing per-session summaries. Use one of three segmentation modes: continuous, cutoff, or gap. The output is one row per session or a session-length sequence; it does not directly return a single scalar. Confirm the session field, section (days), gap (seconds), and which per-session statistic to compute.
tags: [execute, logics, session, segmentation, window, continuous, cutoff, gap, event-segmentation, session-analysis]
related_source: [execute-logics-window, execute-logics-series, execute-metrics-distribution, execute-metrics-time]
priority: 60
---

## 适用场景
适用于需要先把一串记录切成多个会话/窗口，再统计每个会话的特征。它不是直接求一个总值，而是“先分段，再汇总”。

典型场景：
- 按类别或方向变化持续切段，再看每段记录数、累计值、时间跨度。
- 按最近 N 天回溯切分，比较近期与历史两个区段。
- 只要相邻记录时间差超过阈值就切新会话。
- 找出时间跨度最长或记录最多的会话。

> 边界：只需要单值统计（如总时长、平均间隔、峰值）去 `execute-metrics-time`；只需最近 N 天/时间差这类单步时间分段去 `execute-logics-window`；只需逐行差值或命中行去 `execute-logics-series`。本模式是“先切窗 → 再会话内统计”，输出是“每会话一行”或“会话长度序列”。
> 划窗方式与 window 的耦合：continuous（连续同值）本质上是“按列前后取值是否变化”做切分；event（gap）本质上是“相邻时间差是否超过阈值”做切分；cutoff（日期回溯）本质上是“最近 section 天的时间范围”切成两段。切好窗后的 counts/aggregations 仍然是 groupby 的聚合形态。

## 逻辑速查表（话术 → 划窗方式 → pandas 写法）

> `t = pd.to_datetime(df[event_time_col])`；`epoch = t.astype("int64") // 10**9`（pandas 时间戳 int64 是纳秒，÷10⁹ 得 epoch 秒）。所有划窗都依赖时间升序（`presort`）。

### A. 连续同值（continuous）
| 话术 | 划窗方式 | pandas 写法 |
|---|---|---|
| 某个字段不变算同一会话，变化时切分 | 会话标签 SessionId | `(df[session_col] != df[session_col].shift()).cumsum()` |
| 多个字段一起变化时切分 | 会话标签 SessionId | 拼接多列后同上（`df[cols].ne(df[cols].shift()).any(axis=1).cumsum()`） |

> 连续同值划窗按 `session_col` 前后取值是否一致；`session_col` 可以是单列，也可以是多列组合。「category + direction」一起切分是常见用法。

### B. 日期回溯（cutoff）
| 话术 | 划窗方式 | pandas 写法 |
|---|---|---|
| 最近 section 天是“近期”，之前是“历史”，切成两段 | 两段掩码 | `t >= (t.max() - pd.Timedelta(days=section)).normalize()` |

> 以 `max`（最新日期）为末端，向前截 `section` 天。得到近段/历史段两部分；如果截断点落在首尾，整段可能只形成一个会话。只有 count / aggregate 两种统计。

### C. gap 间隔（event）
| 话术 | 划窗方式 | pandas 写法 |
|---|---|---|
| 相邻记录间隔超过 gap 秒就算新会话 | 会话标签 SessionId | `(epoch.diff() > gap).cumsum()` |

> gap 划窗按相邻时间差 `> gap` 秒切新会话。gap 是“最大容忍间隔”，小于等于 gap 的连续记录归同一会话。

### D. 会话内统计（每种划窗方式通用）
| 话术 | 会话内统计 | pandas 写法 |
|---|---|---|
| 每个会话有几条记录 | cnt 事件数 | `df.groupby(session_id).size()` |
| 每个会话累计值 | aggr 数值聚合 | `df.groupby(session_id)[value_col].sum()`（或 `last()` 末值） |
| 会话内跨度 / 会话间隔 | timedelta | 会话内 `max-min`；会话间 `next_min - min` |
| 时间跨度最长的会话 | longest | `idxmax` 取跨度最大值 |
| 记录最多的会话 | largest | `idxmax` 取记录数最大值 |

> 切好窗之后，`groupby(session_id)` 是通用模式；聚合形态（sum/count/mean etc.）可进一步参考 `execute-metrics-distribution`。

## 关系解读与借鉴值
> 这些形态输出的是“会话标签”或“每会话统计”，必须落回具体数据语义。下表给出的是经验解释，不是硬性判断。

| 会话形态 | 数值含义 | 借鉴值 |
|---|---|---|
| 会话标签 SessionId | 每条记录属于哪个会话 | 会话数量多表示行为更碎片化；会话数量少但每个会话很长表示连续集中行为 |
| cnt 事件数 | 每个会话记录数 | 单会话记录数显著增加，可能表示短时间密集处理 |
| aggr 累计值 | 每个会话的数值聚合 | 单会话累计值异常大，可能表示集中值流 |
| timedelta | 会话内跨度 / 会话间隔 | 跨度长表示持续活动；间隔短表示高频重复 |
| longest | 时间跨度最长的会话 | 拖得最久的会话值得单独排查 |
| largest | 记录最多的会话 | 最密集的一段值得重点检查 |

## 参数（口径需先确认）
- `session_col`: continuous 划窗用的列，可为单列或多列。
- `section`: cutoff 回溯天数。
- `gap`: event 的间隔阈值（秒）。
- `event_time_col`: 时间字段，例如 `event_time`。
- `value_col`: 聚合统计的数值字段。
- `session_stat`: 会话内统计类型（cnt / aggr / timedelta / longest / largest）。
- `partition_cols`: 分组键，常见形式 `[date_key, entity_id]`；三种划窗都是按实体分段计算。
- `sort_col` / `order`: 划窗依赖时间升序，需在 `partition` 中设置 `presort`。

## 示例

### 示例一：连续同值划窗 + cnt/aggr
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 6,
    "entity_id": ["e1"] * 6,
    "event_time": ["2023-01-01 10:00:00", "2023-01-01 10:01:00", "2023-01-01 10:02:00",
                   "2023-01-01 10:05:00", "2023-01-01 10:06:00", "2023-01-01 10:07:00"],
    "category": ["in", "in", "in", "out", "out", "in"],
    "value": [100, 200, 150, 300, 250, 50],
})

def continuous_sessions(
    df: pd.DataFrame, partition_cols: List[str], session_col: str, value_col: str
) -> List[Dict[str, Any]]:
    session_id = (df[session_col] != df[session_col].shift()).cumsum()
    cnt = df.groupby(session_id).size()
    total = df.groupby(session_id)[value_col].sum()
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "session_cnt": "|".join(str(v) for v in cnt),
        "session_value": "|".join(str(v) for v in total),
        "n_sessions": int(cnt.size),
    }]

result = execute(
    df,
    continuous_sessions,
    schema="date_key:str,entity_id:str,session_cnt:str,session_value:str,n_sessions:long",
    params=dict(partition_cols=["date_key", "entity_id"], session_col="category", value_col="value"),
    partition={"by": ["date_key", "entity_id"], "presort": "event_time asc"},
)
print(result.to_markdown())
```

说明：
- `category` 序列 `[in, in, in, out, out, in]` 将数据划成三段：`{in,in,in}`、`{out,out}`、`{in}`。
- `session_cnt="3|2|1"`，`session_value="450|550|50"`，`n_sessions=3`。
- 这是连续同值切段：只要前后值变化就切新会话，排序需要按时间升序。

### 示例二：gap 间隔划窗 + largest
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 6,
    "entity_id": ["e1"] * 6,
    "event_time": ["2023-01-01 10:00:00", "2023-01-01 10:02:00", "2023-01-01 10:04:00",
                   "2023-01-01 11:00:00", "2023-01-01 11:01:00", "2023-01-01 13:00:00"],
    "value": [100, 200, 150, 300, 250, 50],
})

def gap_sessions(
    df: pd.DataFrame, partition_cols: List[str], event_time_col: str, value_col: str, gap_sec: int
) -> List[Dict[str, Any]]:
    epoch = pd.to_datetime(df[event_time_col]).astype("int64") // 10**9
    session_id = (epoch.diff() > gap_sec).cumsum()
    cnt = df.groupby(session_id).size()
    largest_id = cnt.idxmax()
    largest_value = df.loc[session_id == largest_id, value_col].sum()
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "session_cnt": "|".join(str(v) for v in cnt),
        "n_sessions": int(cnt.size),
        "largest_id": int(largest_id),
        "largest_value": int(largest_value),
    }]

result = execute(
    df,
    gap_sessions,
    schema="date_key:str,entity_id:str,session_cnt:str,n_sessions:long,largest_id:long,largest_value:long",
    params=dict(partition_cols=["date_key", "entity_id"], event_time_col="event_time", value_col="value", gap_sec=600),
    partition={"by": ["date_key", "entity_id"], "presort": "event_time asc"},
)
print(result.to_markdown())
```

说明：
- `gap=600` 秒（10 分钟）时，相邻记录时间差超过 600 秒就切新会话。
- 数据最终分为 3 个会话：{10:00,10:02,10:04}、{11:00,11:01}、{13:00}，`session_cnt="3|2|1"`。
- `cnt.idxmax()` 取记录最多的会话，`largest_id=0`，`largest_value=450`。

### 示例三：日期回溯划窗 + cnt/aggr
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 5,
    "entity_id": ["e1"] * 5,
    "event_time": ["2023-01-01", "2023-01-10", "2023-01-20", "2023-02-15", "2023-02-20"],
    "value": [100, 200, 150, 300, 250],
})

def cutoff_sessions(
    df: pd.DataFrame, partition_cols: List[str], event_time_col: str, value_col: str, section: int
) -> List[Dict[str, Any]]:
    t = pd.to_datetime(df[event_time_col])
    recent = t >= (t.max() - pd.Timedelta(days=section)).normalize()
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "recent_count": int(recent.sum()),
        "recent_value": int(df.loc[recent, value_col].sum()),
        "history_count": int((~recent).sum()),
        "history_value": int(df.loc[~recent, value_col].sum()),
    }]

result = execute(
    df,
    cutoff_sessions,
    schema="date_key:str,entity_id:str,recent_count:long,recent_value:long,history_count:long,history_value:long",
    params=dict(partition_cols=["date_key", "entity_id"], event_time_col="event_time", value_col="value", section=30),
    partition={"by": ["date_key", "entity_id"]},
)
print(result.to_markdown())
```

说明：
- `section=30` → 最近 30 天从 2023-02-20 往前 30 天起算；近期区段是 {2023-02-15, 2023-02-20}，历史区段是 {2023-01-01, 2023-01-10, 2023-01-20}。
- 近期 `recent_count=2`，`recent_value=550`；历史 `history_count=3`，`history_value=450`。
- cutoff 语义是“最近 section 天 vs 之前”，它只支持针对区段的 count / aggregate 统计，没有 longest / largest / timedelta。

## 生成注意事项
- **先切窗再统计**：三种划窗都产出“会话标签”或“两段掩码”，再用 `groupby(session_id)` 计算 cnt/aggr/timedelta/longest/largest；不要把分段和统计混为一谈。
- **划窗依赖时间升序**：continuous 的 `shift()`、event 的 `diff()`、cutoff 的 `max()` 都依赖顺序；设置 `presort` 很重要。
- **epoch 秒口径**：event 的 gap 是秒，`pd.to_datetime(...).astype("int64") // 10**9` 给出秒级时间戳，再与 `gap_sec` 比较。
- **cutoff 只有 cnt/aggr**：日期回溯只切近期/历史两段，不具备 longest/largest/timedelta 这类临时统计。
- **longest/largest 对并列情况要明确口径**：如果存在并列最大值，默认 `idxmax()` 取第一个；如果需要“最后一个”需要额外显式处理，并在生成前确认。
- **名单/序列/映射必须落库**：会话长度序列要串成字符串；数值要转成整型或字符串；每会话一行输出直接返回 `List[Dict]`。
- 函数中不要写死字段名：使用 `df[session_col]` / `df[event_time_col]` / `df[value_col]`，由 `params` 注入；分组键由 `partition_cols` 透传。
- 不要杜撰不存在的 API；本模式涉及的 pandas 方法（`shift`、`cumsum`、`diff`、`groupby`、`size`、`sum`、`idxmax`、`loc`）均为标准方法。
