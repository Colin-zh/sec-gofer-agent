---
id: execute-metrics-trend
title: Trend Metrics mint.execute Pattern
resource_type: pattern_source
summary: Sequence trend and period-over-period metrics: direction from early to late observations, counts and sums of positive/negative changes, adjacent differences, and percentage changes. These computations apply to any ordered numeric series. Trend and adjacent-change metrics require sorting within each partition; confirm value_col, partition_cols, and the ordering fields.
tags: [execute, metrics, trend, diff, ratio, 趋势, 环比, 差分, 差比, 前高后低, 上升, 下降, 涨跌, 增减]
related_source: [execute-metrics-distribution, execute-metrics-extremum, execute-logics-series]
priority: 70
---

## 指标范围
适用于按时间或其他明确顺序排列的数值序列。涵盖前后水平差、相邻值差分与相邻变化比例；极值和整体分布指标另见 `execute-metrics-extremum` 与 `execute-metrics-distribution`。

## 指标速查表（分析目标 → 指标 → pandas 写法）

> 下表中 `s` 是「按时间升序排序后的任意数值列 Series」（`s = df[value_col]`，也可为其它连续字段）。趋势、环比都必须先 `presort` 排序，否则「前高后低」「相邻」无业务含义。

| 分析目标 | 指标 | pandas 写法 |
|---|---|---|
| 整体上升还是下降 | 序列趋势 Trend | 前后半段均值差 `s.iloc[len(s)//2:].mean() - s.iloc[:len(s)//2].mean()` |
| 统计相邻变化方向与幅度 | 涨跌方向 DiffDirection | `d = s.diff()`；增量数 `(d > 0).sum()`、减量数 `(d < 0).sum()`、正向增量和 `d[d > 0].sum()`、负向变化绝对值和 `-d[d < 0].sum()`、增减数比 = 增量数 / 减量数 |
| 相邻记录差值 | 环比差分 CDiff | `s.diff()`（逐行，首项 NaN） |
| 相邻记录百分比变化 | 环比差比 CDiv | `s.pct_change()`（逐行，首项 NaN） |

> 趋势是**单标量**（每分区一个数）；CDiff/CDiv 是**逐行序列**（shape-preserving），用于「指标」时再聚合（如 `mean`/`max`/`std`），逐行展开的写法见 `execute-logics-series`。
> `s.diff()` 的**差值序列本身是一条数值列**，可继续套 `execute-metrics-distribution` / `execute-metrics-extremum`：`s.diff().max()` = 最大单次正向变化、`s.diff().min()` = 最大单次负向变化、`s.diff().abs().max()` = 最大变化幅度，还可算 `std`/分位数等分布指标。

## 指标解释

| 指标 | 数值含义 | 解释 |
|---|---|---|
| 序列趋势 Trend | >0 前低后高（越到后面越大）；<0 前高后低（越到后面越小）；≈0 平稳 | 「前后半段均值差」最直观（数值单位），可除以整体均值得相对幅度；只看符号判断方向，幅度要结合均值量级 |
| 涨跌方向 DiffDirection | 正向变化数与负向变化数（或其累计幅度）的对比 | 正向变化数较多表示增加方向出现更频繁；正向累计幅度较大表示净变化为正。只比较方向、不比较幅度时，极端单次变化影响较小 |
| 环比差分 CDiff | 相邻记录的数值差，正值为增加、负值为减少 | 看均值/标准差可描述变化幅度；差分带原数值单位，跨组比较时考虑 CDiv。`s.diff()` 可继续接 extremum/distribution |
| 环比差比 CDiv | 相邻记录的比例变化 | 首项为 NaN；前值为 0 时结果可能为无穷或未定义。变化幅度需结合数据范围和测量过程解释，不设通用异常阈值 |

## 参数
- `value_col`: 数值字段名；示例使用 `value`。
- `partition_cols`: 分组字段，由数据集结构和分析粒度决定。
- `sort_col` / `order`: 趋势/环比依赖的排序字段与方向；示例按 `event_time` 升序，通过 `partition` 的 `presort` 传入，**不是函数参数**。

## 示例

### 示例一：序列趋势 + 涨跌方向 + 环比差分/差比（对应速查表全部四行）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 5 + ["2023-01-02"] * 4,
    "group_id": ["group_a"] * 5 + ["group_b"] * 4,
    "event_time": ["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04", "2023-01-05",
                  "2023-01-06", "2023-01-07", "2023-01-08", "2023-01-09"],
    "value": [100, 80, 60, 40, 20, 10, 20, 30, 40],
})

def trend_metrics(
    df: pd.DataFrame, partition_cols: List[str], value_col: str
) -> List[Dict[str, Any]]:
    s = df[value_col]
    half = len(s) // 2
    trend = s.iloc[half:].mean() - s.iloc[:half].mean()
    diff = s.diff()
    chg = s.pct_change()
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "trend": float(trend),
        "up_count": int((diff > 0).sum()),
        "down_count": int((diff < 0).sum()),
        "up_sum": float(diff[diff > 0].sum()),
        "down_sum": float(-diff[diff < 0].sum()),
        "diff_max": float(diff.max()),
        "diff_min": float(diff.min()),
        "chg_mean": float(chg.mean()),
    }]

result = execute(
    df,
    trend_metrics,
    schema="group_date:str,group_id:str,trend:double,up_count:long,down_count:long,up_sum:double,down_sum:double,diff_max:double,diff_min:double,chg_mean:double",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value"),
    partition={"by": ["group_date", "group_id"], "presort": "event_time asc"},
)
print(result.to_markdown())
```

说明：
- `presort: "event_time asc"` 让函数拿到按时间升序的数据，趋势/环比才有意义；同日多记录再加 `event_clock`。
- group_a 数值 `[100,80,60,40,20]`：前后半段均值差 = 40 - 90 = -50，`trend<0` 表示「前高后低」下降；group_b `[10,20,30,40]` 得 +20，上升。
- 涨跌方向：group_a 全跌 → `up_count=0`、`down_count=4`、`up_sum=0`、`down_sum=80`；group_b 全涨 → `up_count=3`、`down_count=0`、`up_sum=30`、`down_sum=0`。涨跌记录数/累计一对比即知整体增多还是减少。
- `s.diff()` 是逐行序列（首项 NaN，`>0`/`<0` 比较自动忽略 NaN）。本例单调：group_a 每次 -20 → `diff_max=diff_min=-20`；非单调时 `diff.max()` = 跨度最大的一次增幅、`diff.min()` = 最大降幅。差值序列本身还可继续套 extremum/distribution。
- 列名不写死：分组键用 `partition_cols` + `**{col: df.iloc[0][col] for col in partition_cols}` 透传，指标列用 `df[value_col]`。

## 实现注意事项
- 趋势/环比**必须配合 `partition` + `presort` 按时间排序**，否则「前高后低」「相邻」无业务含义；排序字段优先 `event_time`，同日多记录再加 `event_clock`。
- 序列趋势用「前后半段均值差」作整体方向摘要；当 `len(s)` 为奇数时，按 `len(s)//2` 分割会使后半段多一条记录。若要等间隔下的斜率，用 `np.polyfit(range(len(s)), s, 1)[0]`；其单位和解释与均值差不同。
- 涨跌记录统计忽略零差值；增减数比在减量数为 0 时未定义或不可直接计算。前后半段均值差和变化方向统计是不同口径，不应相互替代。
- `s.diff()` 的**差值序列本身是一条数值列**，可继续做 distribution/extremum：`s.diff().max()` = 最大单次正向变化、`s.diff().min()` = 最大单次负向变化、`s.diff().abs().max()` = 最大变化幅度，或接 std/分位数等分布指标。
- `s.diff()` / `s.pct_change()` 是**逐行 shape-preserving**操作，首项 NaN；要得「单指标」再聚合（mean/max/std），逐行展开见 `execute-logics-series`。
- `pct_change()` 在前一值为 0 时可能产生无穷或未定义结果；按分析要求处理零值、缺失值和序列长度不足的情况。前后半段均值差要求有足够的有效观测，空序列或全缺失序列需先定义处理方式。
- 聚合/统计结果结构通常与输入不同，**必须写显式 schema**。`schema="*"` 只保留输入已有列，不会声明新列；给每行添加 diff 等列时使用 `schema="*,diff:double"`（按实际类型填写），或写完整输出 schema。不要将新列与 plain `schema="*"` 搭配。
- 函数内**不要写死列名**：一律用 `df[value_col]`，具体字段名经 `params=dict(...)` 注入；分组键用 `partition_cols: List[str]` 接收，返回里用 `**{col: df.iloc[0][col] for col in partition_cols}` 透传。
- `pct_change()` 首项为 NaN，聚合时自动忽略，不要手写 `s / s.shift(1) - 1` 反而引入 NaN 处理复杂度（除非要自定义分母）。
- 不要杜撰不存在的 API；本模式涉及的 pandas/numpy 方法（`diff`/`pct_change`/`polyfit`/均值）均为标准方法。
