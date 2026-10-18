---
id: execute-logics-graph
title: Graph / Relation Logic mint.execute Pattern
resource_type: pattern_source
summary: Reusable graph/relation logic for direction-aware relationships among related entities. It covers de-duplicated related entities, one-off related entities, direction-based sets, top-N related IDs, bidirectional vs one-way relations, per-entity direction composition, and near-zero net-flow relationships. Use this when the output is a collection or a relation summary for a single entity, not a global graph across many entities.
tags: [execute, logics, graph, relation, direction, related-entities, graph-logic, in-out, net-flow, set-logic]
related_source: [execute-metrics-distribution, execute-logics-series, execute-logics-graph-relation, execute-metrics-time]
priority: 60
---

## 适用场景
适用于“实体之间的关系”处理，而不是单值指标。核心是：按方向（入/出）对相关实体集合做集合运算，并输出关系摘要。

典型场景：
- 哪些相关实体同时出现在入站和出站方向上？
- 哪些相关实体只出现于入站 / 只出现于出站？
- 哪些相关实体只出现过一次？
- 某个方向中金额最大或交易最频繁的前 N 个相关实体是谁？
- 哪个相关实体在两方向上的金额几乎相等？

> 边界：如果只关心整体分布/集中度，请转到 `execute-metrics-distribution`；如果想把列表/序列展开成每个相关实体一行，请转到 `execute-logics-series`；如果想看跨实体之间的网络结构（共享相关实体、枢纽、连通分量），请转到 `execute-logics-graph-relation`。本模式是“主体视角”的单跳关系：在 `partition` 按 `date_key + entity_id` 分组后，函数只看到“一个实体的记录”，聚合的是“此实体与哪些相关实体有哪些方向关系”。
> 本模式不是全局图算法；它只处理单个实体或分组内的关系集合，不做实体间全量连通性计算。

## 逻辑速查表（话术 → 对手关系 → pandas 写法）

> 下表中 `d = df[direction_col]`；`in_ids = df.loc[d == in_val, related_id_col].dropna().unique()`；`out_ids = df.loc[d == out_val, related_id_col].dropna().unique()`；`vc = df[related_id_col].value_counts()`。

### A. 相关实体集合（不分方向）
| 话术 | 关系 | pandas 写法 |
|---|---|---|
| 一共涉及哪些相关实体 | 去重相关实体 AllRelated | `df[related_id_col].dropna().unique().tolist()` |
| 只出现过一条记录的相关实体 | 仅一笔相关实体 OnlyOnceRelated | `vc[vc == 1].index.tolist()` |

### B. 方向过滤（入 / 出）
| 话术 | 关系 | pandas 写法 |
|---|---|---|
| 该方向去重后的相关实体 | 方向去重相关实体 DirectionRelated | `df.loc[d == dir, related_id_col].dropna().unique().tolist()` |
| 该方向金额最大的 N 个相关实体 | 方向内 topN 金额相关实体 TopNAmountRelated | `df.loc[d == dir].groupby(related_id_col)[value_col].sum().nlargest(n).index.tolist()` |
| 该方向交易最频繁的 N 个相关实体 | 方向内 topN 记录数相关实体 TopNCountRelated | `df.loc[d == dir].groupby(related_id_col).size().nlargest(n).index.tolist()` |

### C. 双向 / 单向
| 话术 | 关系 | pandas 写法 |
|---|---|---|
| 同时在入方向和出方向出现的相关实体 | 双向相关实体 BothDirectionRelated | `set(in_ids) & set(out_ids)` |
| 只出现在入方向（或只出现在出方向） | 单向相关实体 OneDirectionRelated | `set(in_ids) - set(out_ids)`（只入）；`set(out_ids) - set(in_ids)`（只出） |

### D. 相关实体的方向组成（逐实体）
| 话术 | 关系 | pandas 写法 |
|---|---|---|
| 每个相关实体的净流入比例 | 入方向占比 InRatio | `g[in_val] / (g[in_val] + g[out_val])` |
| 每个相关实体的净流入 | 净流入 NetFlow | `g[in_val] - g[out_val]` |
| 每个相关实体的出入比 | 出入比 InOutRatio | `g[in_val] / g[out_val]` |

> 这里的 `g` 是每个相关实体 × 方向的金额汇总：`g = df.pivot_table(index=related_id_col, columns=direction_col, values=value_col, aggfunc="sum", fill_value=0)`。

### E. 等额对冲（净流量接近 0）
| 话术 | 关系 | pandas 写法 |
|---|---|---|
| 入方向累计 ≈ 出方向累计的相关实体 | 等额对冲相关实体 HedgeRelated | `net = g[in_val] - g[out_val]`；`hedge = g.index[net.abs() <= tol]` |

> 组合逻辑是自由组合的：例如「单次单向」= 单向 ∩ `vc[vc == 1]`；「前 N 个双向」= 双向 ∩ 方向内 topN；需要时再组合即可。
> 方向取值口径要确认：`direction_col` 中可能是 `in`/`out`、`1`/`2` 或 `C`/`D` 等，需要通过 `in_val` / `out_val` 参数注入。
> `related_id_col` 允许为 `None`，建集前需要 `dropna()`；`set` 运算天然去重。
> 上述关系和排名很可能依赖金额大/笔数高两种口径，生成前需确认是按金额还是按记录数排序。

## 关系解读与借鉴值
> 这些关系输出的是名单、集合、比例或净流向，必须转回具体数据语义。下表给出经验解释，不是硬性判断。

| 关系 | 数值含义 | 借鉴值 |
|---|---|---|
| 去重相关实体 AllRelated | 实体涉及的所有相关实体 | 相关实体越多，网络越分散；相关实体较少，则更集中 |
| 仅一笔相关实体 OnlyOnceRelated | 只出现一次的相关实体 | 大量一次性实体可能表现为试探性或临时关联 |
| 方向去重相关实体 | 单侧相关实体集合 | 入/出方向不对称，可提示不同的流向结构 |
| 方向内 topN 相关实体 | 单侧金额大/高频实体名单 | 定位大头实体和高频连接 |
| 双向相关实体 BothDirection | 同时出现在两侧的实体 | 常见于双向循环、回流或中转结构 |
| 单向相关实体 OneDirection | 只入/只出 | “只出不进”可提示单边流出；“只进不出”可提示单边汇入 |
| 入方向占比 InRatio | 实体流向偏向（0~1） | 0=纯出站、1=纯入站、0.5=双向均衡 |

## 参数（口径需先确认）
- `direction_col`: 方向字段，例如 `direction`。
- `in_val` / `out_val`: 入站/出站取值常见为 `in` / `out`，也可能是 `1/2`、`C/D` 等。
- `related_id_col`: 相关实体字段，例如 `related_id`；`None` 表示相关实体未知。
- `value_col`: 数值字段，例如 `value`。
- `partition_cols`: 分组字段，默认 `[date_key, entity_id]`。
- `top_n`: 方向内 topN 的 N，默认 3。
- `tol`: 等额对冲容差，满足 `abs(入方向累计 - 出方向累计) <= tol` 时判定为对冲，默认根据金额量级选择。

## 示例

### 示例一：相关实体集合 + 方向过滤 + 方向内 topN（按金额 / 按记录数两口径）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 6,
    "entity_id": ["e1"] * 6,
    "direction": ["in", "in", "in", "out", "out", "out"],
    "related_id": ["r1", "r1", "r2", "r3", "r4", "r4"],
    "value": [100, 50, 200, 80, 300, 100],
})

def related_sets(
    df: pd.DataFrame,
    partition_cols: List[str],
    related_id_col: str,
    value_col: str,
    direction_col: str,
    in_val: str,
    out_val: str,
    top_n: int,
) -> List[Dict[str, Any]]:
    d = df[direction_col]
    in_mask = d == in_val
    out_mask = d == out_val

    all_related = df[related_id_col].dropna().unique()
    vc = df[related_id_col].value_counts()
    only1 = vc[vc == 1].index.tolist()

    in_related = df.loc[in_mask, related_id_col].dropna().unique()
    out_related = df.loc[out_mask, related_id_col].dropna().unique()

    in_top_amt = df.loc[in_mask].groupby(related_id_col)[value_col].sum().nlargest(top_n).index.tolist()
    in_top_cnt = df.loc[in_mask].groupby(related_id_col).size().nlargest(top_n).index.tolist()
    out_top_amt = df.loc[out_mask].groupby(related_id_col)[value_col].sum().nlargest(top_n).index.tolist()
    out_top_cnt = df.loc[out_mask].groupby(related_id_col).size().nlargest(top_n).index.tolist()

    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "all_related": "|".join(sorted(all_related)),
        "all_count": int(len(all_related)),
        "only1_related": "|".join(sorted(only1)),
        "only1_count": int(len(only1)),
        "in_related": "|".join(sorted(in_related)),
        "in_count": int(len(in_related)),
        "out_related": "|".join(sorted(out_related)),
        "out_count": int(len(out_related)),
        "in_top_amt": "|".join(in_top_amt),
        "in_top_cnt": "|".join(in_top_cnt),
        "out_top_amt": "|".join(out_top_amt),
        "out_top_cnt": "|".join(out_top_cnt),
    }]

result = execute(
    df,
    related_sets,
    schema="date_key:str,entity_id:str,all_related:str,all_count:long,only1_related:str,only1_count:long,in_related:str,in_count:long,out_related:str,out_count:long,in_top_amt:str,in_top_cnt:str,out_top_amt:str,out_top_cnt:str",
    params=dict(partition_cols=["date_key", "entity_id"], related_id_col="related_id", value_col="value", direction_col="direction", in_val="in", out_val="out", top_n=1),
    partition={"by": ["date_key", "entity_id"]},
)
print(result.to_markdown())
```

说明：
- 去重相关实体 = `dropna().unique()`；只出现一次 = `value_counts() == 1`；方向过滤用 `d == in_val` / `d == out_val`。
- `topN` 两口径不同：按金额排名与按记录数排名可能产生不同结果，所以在生成前先确认要看金额大还是记录数高。
- e1 的入站相关实体为 {r1, r2}；出站相关实体为 {r3, r4}；`in_top_amt` 和 `in_top_cnt` 可能对应不同的 Top 1。

### 示例二：双向 + 单向 + 一次性单向
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 6,
    "entity_id": ["e1"] * 6,
    "direction": ["in", "in", "in", "out", "out", "in"],
    "related_id": ["r1", "r2", "r2", "r1", "r3", "r4"],
    "value": [100, 200, 300, 50, 80, 10],
})

def relation_sets(
    df: pd.DataFrame,
    partition_cols: List[str],
    related_id_col: str,
    direction_col: str,
    in_val: str,
    out_val: str,
) -> List[Dict[str, Any]]:
    d = df[direction_col]
    in_related = set(df.loc[d == in_val, related_id_col].dropna().unique())
    out_related = set(df.loc[d == out_val, related_id_col].dropna().unique())
    vc = df[related_id_col].value_counts()

    both = in_related & out_related
    only_in = in_related - out_related
    only_out = out_related - in_related
    only1_in = sorted(o for o in only_in if vc.get(o, 0) == 1)

    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "both_related": "|".join(sorted(both)),
        "both_count": int(len(both)),
        "only_in_related": "|".join(sorted(only_in)),
        "only_in_count": int(len(only_in)),
        "only_out_related": "|".join(sorted(only_out)),
        "only_out_count": int(len(only_out)),
        "only1_in_related": "|".join(only1_in),
        "only1_in_count": int(len(only1_in)),
    }]

result = execute(
    df,
    relation_sets,
    schema="date_key:str,entity_id:str,both_related:str,both_count:long,only_in_related:str,only_in_count:long,only_out_related:str,only_out_count:long,only1_in_related:str,only1_in_count:long",
    params=dict(partition_cols=["date_key", "entity_id"], related_id_col="related_id", direction_col="direction", in_val="in", out_val="out"),
    partition={"by": ["date_key", "entity_id"]},
)
print(result.to_markdown())
```

说明：
- 双向 = 集合交集 `in_related & out_related`。
- 单向 = `in_related - out_related` / `out_related - in_related`。
- “一次性单向”是组合逻辑：单向 ∩ 只出现一次。

### 示例三：方向构成 + 等额对冲
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "date_key": ["2023-01-01"] * 5,
    "entity_id": ["e1"] * 5,
    "direction": ["in", "in", "out", "in", "out"],
    "related_id": ["r1", "r1", "r1", "r2", "r3"],
    "value": [100, 100, 190, 300, 50],
})

def hedge_related(
    df: pd.DataFrame,
    partition_cols: List[str],
    related_id_col: str,
    value_col: str,
    direction_col: str,
    in_val: str,
    out_val: str,
    tol: float,
) -> List[Dict[str, Any]]:
    g = df.pivot_table(
        index=related_id_col, columns=direction_col, values=value_col,
        aggfunc="sum", fill_value=0,
    ).reindex(columns=[in_val, out_val], fill_value=0)
    in_sum = g[in_val]
    out_sum = g[out_val]
    net = in_sum - out_sum
    hedge_mask = net.abs() <= tol
    hedge_related = g.index[hedge_mask].tolist()
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "hedge_related": "|".join(sorted(hedge_related)),
        "hedge_count": int(len(hedge_related)),
    }]

result = execute(
    df,
    hedge_related,
    schema="date_key:str,entity_id:str,hedge_related:str,hedge_count:long",
    params=dict(partition_cols=["date_key", "entity_id"], related_id_col="related_id", value_col="value", direction_col="direction", in_val="in", out_val="out", tol=10),
    partition={"by": ["date_key", "entity_id"]},
)
print(result.to_markdown())
```

说明：
- `g` 表示每个相关实体在两方向上的累计金额。
- 只要 `abs(in_sum - out_sum) <= tol`，就视为近似对冲。
- 这类关系是“净流入接近 0”的对冲型实体，不等同于“双向相关实体”。二者是两个不同的判断口径。

## 生成注意事项
- **视角别搞混**：本模式是单实体视角的关系集合；如果要看跨实体网络（共享相关实体、枢纽、连通分量），请转到 `execute-logics-graph-relation`。
- **名单/序列类结果必须落库**：`set` / `list` 必须写成字符串列（`"|".join(sorted(...))`）或数量列。
- **相关实体去重**：`dropna()` + `astype(str).str.strip() != ""` 处理空值和空字符串；否则空值会被当成真实相关实体。
- **方向取值口径必须确认**：`in`/`out`、`1`/`2`、`C`/`D` 等都可能出现，生成前必须明确 `in_val` / `out_val`。
- **topN 的口径要确认**：按金额排名和按记录数排名可能完全不同；两者都允许，不要默认假定它们一致。
- **同时出现双向/单向/对冲**：这三类并不是同一个判定逻辑：双向是集合重合，单向是集合差，对冲是金额接近平衡。
- 函数中不要写死字段名：使用 `df[related_id_col]` / `df[value_col]` / `df[direction_col]`；输出字段名使用固定语义（如 `hedge_related`, `both_related`）。
- 不要杜撰 API；本模式依赖 pandas 标准方法：`dropna`、`unique`、`value_counts`、`groupby`、`pivot_table`、`nlargest` 等。
