---
id: execute-logics-series
title: Series / Sequence Logic mint.execute Pattern
resource_type: pattern_source
summary: Reusable sequence logic for record-level operations such as selecting matching rows, deriving adjacent differences, and identifying multiple-value relationships among related entities. Use this pattern when the output is a sequence, a list of row indexes, or a list of related IDs rather than a single aggregated scalar. Core building blocks are adjacent differences, condition-based filtering, and multiple-value checks. Keep the record order stable and confirm sort_col, value_col, related_id_col, and the multiple-value rule.
tags: [execute, logics, series, sequence, diff, filter, multiple, records, adjacent-difference, condition-filtering, related-entities]
related_source: [execute-metrics-trend, execute-metrics-distribution, execute-metrics-extremum, execute-logics-graph]
priority: 60
---

## 适用场景
适用于需要在逐行/名单层面处理数据：哪些记录命中、命中值是什么、相邻记录之间的差异、以及多个相关实体是否满足倍数或比例关系。它不是单一聚合指标模式。

典型场景：
- 逐行比较当前值与上一条记录之间的差值或涨跌百分比。
- 找出满足条件的记录，以及这些记录的字段值分别是什么。
- 判断相关实体的累计值之间是否存在整数倍或近似倍关系。

> 边界：如果你需要单个标量（均值、最大值、总和、分布摘要），请转到 `execute-metrics-*`；如果你需要图/关系结构（双向/单向/共同关联、节点与边），请转到 `execute-logics-graph` 或 `execute-logics-graph-relation`。本模式输出的是序列、名单、索引或逐行派生字段，不是聚合结果。
> 与 metrics 的关系：这里的系列操作对应 `execute-metrics-trend`（差值/比例差）和 `execute-metrics-distribution`（真值条件计数/占比）等指标的逐行或名单表达式。只在输出需要序列/名单时使用本模式。

## 逻辑速查表（业务话术 → 序列形态 → pandas 写法）

> `s = df[value_col]`，在 `presort` 之后执行 `s.diff()` / `s.pct_change()`；`cond` 是布尔掩码；`g` 是每个 related_id 的累计值。

### A. 相邻差（逐行序列）
| 话术 | 序列形态 | pandas 写法 |
|---|---|---|
| 当前记录与上一条记录的差值 | 相邻差 DiffSeries | `s.diff()` |
| 当前记录与上一条记录的涨跌百分比 | 相邻差比 RatioSeries | `s.pct_change()` |

> 这是 shape-preserving 的逐行数字序列；如需数值汇总（平均增幅、最大增幅等），请转到 `execute-metrics-trend`。差值序列本身可以继续按 distribution / extremum 操作。

### B. 条件定位（命中记录 + 取值）
| 话术 | 序列形态 | pandas 写法 |
|---|---|---|
| 哪些记录满足条件 | 命中索引 FilterIndexes | `cond` / `df.index[cond]` |
| 命中记录的某列值 | 命中取值 FilterList | `df.loc[cond, col]` |
| 命中记录的多列值 | 命中成对 FilterPairs | `df.loc[cond, [value_col, category_col]]` |

> 这是「条件筛选」的名单/索引形态；如果需要计数或占比，请转到 `execute-metrics-distribution` 中的 true_* 形态（`cond.sum()` / `cond.mean()`）。

### C. 倍数关系（按累计值判定）
| 话术 | 序列形态 | pandas 写法 |
|---|---|---|
| 两个相关实体的累计值之间存在倍数关系 | MultipleRelatedEntities | `g = df.groupby(related_id_col)[value_col].sum()`；两两判 `max(a, b) % min(a, b) == 0` |

> 判定规则需先确认：整数倍 `max % min == 0`（默认严格）、近似倍 `abs(max/min - round(max/min)) <= tol`（容忍舍入）、或者固定底数（先定底、再找倍数）。默认口径是“任意两条累计值为整数倍”。

## 关系解读与借鉴值
> 这些形态输出的是序列、名单或索引，必须落回具体业务语义；下表提供的是经验借鉴，不是硬性结论。

| 序列形态 | 数值含义 | 借鉴值 |
|---|---|---|
| 相邻差 DiffSeries | 按顺序排列的逐行差值（正数=增大，负数=减小） | 整条序列可看到异常跳变；首项为 NaN |
| 相邻差比 RatioSeries | 逐行涨跌百分比 | 如果绝对值突增，说明出现明显跳变 |
| 命中索引 / 取值 FilterIndexes / FilterList | 满足条件的记录和字段值 | 可用于进一步筛选、分段、合并或出图 |
| 倍数关系 MultipleRelatedEntities | 累计值之间存在倍数关系 | 这常提示共用底数、拆分、归并或对齐模式，需结合上下文再判定 |

## 参数（口径需先确认）
- `value_col`: 数值字段，例如 `value`。
- `related_id_col`: 相关实体字段，例如 `related_id`。
- `category_col`: 分类字段（如 `category`，用于 filter pairs）。
- `sort_col` / `order`: `diff` / `pct_change` 依赖的排序字段，需在 `partition` 中使用 `presort`。
- `cond`: 布尔筛选条件（来源不限）。
- `multiple_base` / `multiple_tol`: 倍数底、近似倍容差；可选。

## 示例

### 示例一：相邻差序列（shape-preserving 加列）
```python
import pandas as pd
from mint import execute

df = pd.DataFrame({
    "entity_id": ["e1"] * 4,
    "event_time": ["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04"],
    "value": [100, 200, 50, 300],
})

def add_diff_columns(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    s = df[value_col]
    out = df.copy()
    out["value_diff"] = s.diff()          # 逐行差（首项 NaN）
    out["value_pct"] = s.pct_change()     # 逐行涨跌百分比（首项 NaN）
    return out

result = execute(
    df,
    add_diff_columns,
    schema="*,value_diff:double,value_pct:double",
    params=dict(value_col="value"),
    partition={"by": ["entity_id"], "presort": "event_time asc"},
)
print(result.to_markdown())
```

说明：
- 这是 shape-preserving 输出，每行新增两列，保留原有记录结构；`Schema.transform("*")` 只表示保留输入中的现有列，不包含函数新增的列，因此新增列时要用 `schema="*,value_diff:double,value_pct:double"`，或声明完整输出 schema。
- `value=[100,200,50,300]` 时，`value_diff=[NaN,100,-150,250]`，`value_pct=[NaN,1.0,-0.75,5.0]`。
- 如果需要单个标量（例如平均增幅、最大增幅、上升/下降笔数），请转到 `execute-metrics-trend`，不在此处聚合。

### 示例二：条件定位（命中记录 + 取值）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "entity_id": ["e1"] * 5,
    "related_id": ["r1", "r2", "r3", "r4", "r5"],
    "value": [100, 200, 80, 300, 50],
})

def filter_hits(
    df: pd.DataFrame, partition_cols: List[str], value_col: str, related_id_col: str
) -> List[Dict[str, Any]]:
    cond = df[value_col] > 100                       # 条件：值 > 100
    hit_related = df.loc[cond, related_id_col].tolist()
    hit_values = df.loc[cond, value_col].tolist()
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "hit_related": "|".join(hit_related),
        "hit_count": int(len(hit_related)),
        "hit_values": "|".join(str(v) for v in hit_values),
    }]

result = execute(
    df,
    filter_hits,
    schema="entity_id:str,hit_related:str,hit_count:long,hit_values:str",
    params=dict(partition_cols=["entity_id"], value_col="value", related_id_col="related_id"),
    partition={"by": ["entity_id"]},
)
print(result.to_markdown())
```

说明：
- `cond = df[value_col] > 100` 过滤出大于 100 的记录。
- `df.loc[cond, related_id_col]` 和 `df.loc[cond, value_col]` 取命中记录的相关对象和值。
- e1 命中 {r2(200), r4(300)} → `hit_related="r2|r4"`，`hit_count=2`，`hit_values="200|300"`。
- 如果需要的是“满足条件的记录数/占比”，请转到 `execute-metrics-distribution` 的 true_* 形态。

### 示例三：倍数关系（累计值成立倍数）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "entity_id": ["e1"] * 4,
    "related_id": ["r1", "r2", "r3", "r4"],
    "value": [128, 256, 640, 100],
})

def multiple_related(
    df: pd.DataFrame, partition_cols: List[str], related_id_col: str, value_col: str
) -> List[Dict[str, Any]]:
    g = df.groupby(related_id_col)[value_col].sum()
    relateds = g.index.tolist()
    pairs = []
    for i in range(len(relateds)):
        for j in range(i + 1, len(relateds)):
            a, b = g[relateds[i]], g[relateds[j]]
            if a <= 0 or b <= 0:
                continue
            hi, lo = max(a, b), min(a, b)
            if hi % lo == 0:
                pairs.append(f"{relateds[i]}:{relateds[j]}={int(hi / lo)}x")
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "multiple_pairs": "|".join(pairs),
        "multiple_count": int(len(pairs)),
    }]

result = execute(
    df,
    multiple_related,
    schema="entity_id:str,multiple_pairs:str,multiple_count:long",
    params=dict(partition_cols=["entity_id"], related_id_col="related_id", value_col="value"),
    partition={"by": ["entity_id"]},
)
print(result.to_markdown())
```

说明：
- `g` 为每个相关实体累计值：r1=128、r2=256、r3=640、r4=100。
- 两两判定整数倍关系：r1:r2=2x，r1:r3=5x；r2:r3=2.5x 不是整数倍；r4 与其他值无整数倍关系。
- 判定口径（整数倍 / 近似倍 / 固定底数）需在生成前确认；对相关实体较多时，双重循环 O(N²) 会增加计算量。

## 生成注意事项
- **先取序列再决定逻辑**：`df[col]` / `df.loc[cond, col]` 是任何方法的第一步，不应把它视为“聚合逻辑”。
- **分组聚合与频数属于 metrics**：`groupby(...).agg(...)`、`value_counts()` 的聚合形态属于 `execute-metrics-distribution` / `execute-metrics-extremum`，本模式仅保留逐行/名单形态。
- **一次性归图/关系逻辑**：先条件过滤后再做“仅一次/仅单向/共同相关”这类组合逻辑，通常进入 `execute-logics-graph` 处理。
- **shape-preserving 新增列要声明输出列**：`schema="*"` 只保留输入中已有的列，不包含计算函数添加的列；使用 `schema="*,new_col:type"`，或声明完整输出 schema。仅未新增列且输出列与输入完全一致时才使用 `schema="*"`。
- **diff / pct_change 依赖排序**：需要时间升序或业务序；首个值为 NaN，这是正常的。
- **倍数关系使用双重比较**：对相关对象较多时要注意 O(N²)；需要确认是否必须使用严格整数倍、近似倍或固定底数。
- 函数中不要写死字段名：使用 `df[value_col]`、`df[related_id_col]`、`df[category_col]` 等由 `params` 注入；返回中使用固定语义字段名。
- 不要杜撰 API；本模式仅依赖 pandas 标准方法：`diff`、`pct_change`、`loc`、`groupby`、`sum`、`value_counts` 等。
