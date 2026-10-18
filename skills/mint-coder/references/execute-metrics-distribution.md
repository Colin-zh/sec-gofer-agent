---
id: execute-metrics-distribution
title: Distribution & Concentration Metrics mint.execute Pattern
resource_type: pattern_source
summary: Reusable aggregations and distribution/concentration metrics for numeric series, event records, and categories. Numeric metrics include mean, standard deviation, variance, coefficient of variation, quantiles, IQR, skew, kurtosis, mean absolute deviation, outlier count/ratio, and extremum ratios; categorical metrics include mode, ratios, entropy, normalized entropy, and top-N summaries by record count, value, or distinct entity count. Confirm partition_cols, value_col, entity_id_col, and any condition-specific denominator using the available dataset schema.
tags: [execute, metrics, distribution, concentration, ratio, entropy, top-n, top-k, 分布, 集中度, 均匀度, 占比, 主导, 长尾]
related_source: [execute-metrics-extremum, execute-metrics-location, execute-logics-series, execute-logics-graph, execute-logics-window]
priority: 85
---

## 指标范围
适用于按分区汇总事件记录、描述数值列的分布，或衡量类别频率与集中程度。可用于任意领域的数据；分区键和分析字段由调用方根据数据集结构指定。

## 指标速查表（分析目标 → 指标 → pandas 写法）

### A. 基础指标（数值 / 记录数 / 实体数）
| 分析目标 | 指标 | pandas 写法 |
|---|---|---|
| 数值总和 | 求和 Sum | `df[value_col].sum()` |
| 记录数 | 计数 Count | `len(df)` / `df.shape[0]` |
| 不同实体数 | 去重计数 KeyCount | `df[entity_id_col].nunique()` |

### B. 数值列分布指标（针对数值等连续列）
| 分析目标 | 指标 | pandas 写法 |
|---|---|---|
| 平均数值 | 均值 Mean | `s.mean()` |
| 相对均值的离散程度 | 变异系数 Cv | `s.std() / s.mean()` |
| 记录值的绝对离散程度 | 标准差 Std / 方差 Var | `s.std()` / `s.var()` |
| 数值落在哪个分位水平 | 分位数 Quantile | `s.quantile(0.25)`；多分位点 `s.quantile([0.25, 0.5, 0.75])` |
| 中间一半记录的数值跨度 | 分位距 Iqr | `s.quantile(0.75) - s.quantile(0.25)` |
| 满足某条件的记录数 | 真值计数 TrueCount | `cond.sum()` |
| 满足某条件的记录数占比 | 真值占比 TrueRatio | `cond.mean()` / `cond.sum() / len(df)` |
| 满足某条件的数值占比 | 真值数值占比 | `df.loc[cond, value_col].sum() / df[value_col].sum()` |
| 满足某条件的实体数占比 | 真值实体占比 | `df.loc[cond, entity_id_col].nunique() / df[entity_id_col].nunique()` |
| 分布是否偏斜 | 偏度 Skew | `s.skew()` |
| 尾部或峰态相对正态分布的形状 | 峰度 Kurt | `s.kurt()` |
| 平均每记录偏离平均数值多少 | 平均绝对离差 Mad | `(s - s.mean()).abs().mean()` |
| 离群记录数 / 占比 | 异常值计数 OutlierCount / 占比 OutlierRatio | `(np.abs(s - s.mean()) > 3*s.std()).sum()` / `.mean()` |
| 最大记录值占数值总和的比例 | 最大值占比 MaxRatio | `s.max() / s.sum()` |
| 最小记录值占数值总和的比例 | 最小值占比 MinRatio | `s.min() / s.sum()` |
| 所有最大值记录合计占数值总和的比例 | 最大值合计占比 MaxTotalRatio | `s.max() * (s == s.max()).sum() / s.sum()` |
| 所有最小值记录合计占数值总和的比例 | 最小值合计占比 MinTotalRatio | `s.min() * (s == s.min()).sum() / s.sum()` |
| 前 K 大记录值占数值总和的比例 | 前 K 大值占比 TopKValueRatio | `s.nlargest(k).sum() / s.sum()` |

> 上表中 `s` 表示任意连续数值 Series；`cond` 表示任意布尔筛选掩码，例如基于时间范围、事件类型或状态类别。「均值上下」也是一种真值类：`cond = s > s.mean()`、`s < s.mean()` 或 `s == s.mean()`；`df.loc[cond, value_col].sum()` 可计算命中记录的数值和。TopKValueRatio 按数值排序取前 K 条，与 section C 按类别频数排序后计算 top-N 类别数值占比不同。MaxRatio 是 K=1 的特例，MaxTotalRatio 计入所有并列最大值。
> 真值类（TrueCount / TrueRatio / 真值数值占比 / 真值实体占比）是条件筛选的**计数/占比形态**；逐行返回命中记录或其值时，参见 `execute-logics-series` 中的条件定位实现。

### C. 类别字段集中度指标
| 分析目标 | 指标 | pandas 写法 |
|---|---|---|
| 最常见的类别 | 众数 ModeCount | `vc = s.value_counts(); vc.index[0]` |
| 众数占多少 | 众数占比 ModeRatio | `vc.iloc[0] / vc.sum()` |
| 最常见类别的数值占比 | 类别数值占比 | `df.loc[df[cat_col] == vc.index[0], value_col].sum() / df[value_col].sum()` |
| 最常见类别覆盖的实体数占比 | 类别实体数占比 | `df.loc[df[cat_col] == vc.index[0], entity_id_col].nunique() / df[entity_id_col].nunique()` |
| 前 N 占多少（帕累托式） | top-N 记录数占比 | `vc.head(n).sum() / vc.sum()` |
| 前 N 类别的数值占比 | top-N 数值占比 | `df.loc[df[cat_col].isin(vc.head(n).index), value_col].sum() / df[value_col].sum()` |
| 前 N 类别的实体数占比 | top-N 实体数占比 | `df.loc[df[cat_col].isin(vc.head(n).index), entity_id_col].nunique() / df[entity_id_col].nunique()` |
| 均匀度（越低越集中） | 熵 Entropy | `-(p*np.log(p)).sum()` |
| 均匀度 0~1（0 集中 / 1 均匀） | 标准熵 NormalizedEntropy | `entropy / np.log(n)` |
| 频数最高的类别及其名单 | top-N 类别 | `vc.head(n).index.tolist()` |

> 上表中 `s` 表示「任意类别列 Series」（`df[cat_col]`），`vc = s.value_counts()` 为频数分布（默认按频数降序，最频繁的排最前），`p = vc / vc.sum()` 为频率。
> 注意众数的并列问题：`value_counts()` 并列时顺序**不保证稳定**，`vc.index[0]` 取众数在并列时会任意取一个。要判断是否有并列众数，用 `s.mode()`（返回全部并列众数，`len(s.mode()) > 1` 即并列）；要确定性 tie-break，显式加规则（如 `vc.sort_index()` 字典序）。
> 类别集中度可分别看**记录数占比**、**数值占比**、**实体数占比**，三者可能不一致。单类别占比把 `vc.index[0]` 换成任意 `cat`（布尔掩码 `df[cat_col] == cat`）；top-N 占比用 `df[cat_col].isin(vc.head(n).index)` 命中前 N 个类别。计算口径与 section B 的条件筛选指标一致。

### 复杂指标解释

> 下表中的阈值只是常见的粗略参考，不是通用判定标准。解释时要结合样本量、数值尺度、分布形态及数据采集方式；不同数据集之间不应直接套用固定阈值。

| 指标 | 数值含义 | 解释 |
|---|---|---|
| 变异系数 Cv | 相对均值的离散度；仅在均值有意义且非零时适用 | 可把 <0.15、0.15~0.5、>0.5 作为初步描述区间；不是普适分级。不同样本量、均值或数据类型下需重新判断 |
| 标准差 Std / 方差 Var | 绝对离散度，单位与数值一致 | 不可跨量纲比较；跨组比较前应检查数据定义和尺度，Cv 也只适用于均值有意义且非零的情况 |
| 分位距 Iqr | 中间 50% 记录的数值跨度 | 不描述尾部；可与分位数或明确选定的离群规则一起使用。Iqr 大表示中间部分较分散 |
| 偏度 Skew | 大于 0 右偏；等于 0 对称；小于 0 左偏 | 可用绝对值 <0.5、0.5~1、≥1 作粗略描述区间；这些经验分级并非异常判定标准。结合样本量和测量过程解释 |
| 峰度 Kurt（超额峰度） | 大于 0 表示相对正态分布更尖或尾部更重；等于 0 表示超额峰度与正态分布相同；小于 0 表示相对更平坦或尾部更轻 | `pandas` 的 `kurt()` 返回超额峰度（正态分布为 0）。具体含义取决于样本分布；不应仅凭符号断定异常 |
| 平均绝对离差 Mad | 平均每记录偏离均值的绝对数值，单位与数值一致 | 比标准差更稳健（受极端值影响小）；值越大越离散 |
| 标准熵 NormalizedEntropy | 0 = 记录数全集中于一个类别；1 = 各类别频率完全均匀 | 数值越小越集中、越接近 1 越均匀；区间解释应结合类别数和样本量 |
| 熵 Entropy | 绝对均匀度，随类别数增加而增大 | 仅在类别数相同的分布间直接比较；类别数不同时可参考标准熵 |
| MaxRatio / MinRatio | 最大 / 最小单条记录值占数值总和的比例 | `s.max() / s.sum()` / `s.min() / s.sum()`，最大值按一次计，不乘出现次数 |
| MaxTotalRatio | 所有等于最大值的记录值合计占数值总和的比例 | `s.max() * (s == s.max()).sum() / s.sum()`；与 MaxRatio 的区别：MaxRatio 只看单条最大值，MaxTotalRatio 把并列最大值全部计入 |
| MinTotalRatio | 所有等于最小值的记录值合计占数值总和的比例 | `s.min() * (s == s.min()).sum() / s.sum()`；与 MinRatio 的区别：MinRatio 只看单条最小值，MinTotalRatio 把并列最小值全部计入 |
| TopKValueRatio | 数值最大的 K 条记录值合计占数值总和的比例 | K 是分析参数；比例越高，前 K 条记录对总和的贡献越大。与 MaxRatio（K=1）、MaxTotalRatio（所有并列最大值）及 section C 的类别 top-N 数值占比互补 |

> 极差 Ptp = max - min（见 execute-metrics-extremum）会被单个极端值拉大；可将 **Ptp/Iqr 比值** 作为尾部形状的补充描述。比值大小依赖分布和样本，不能单独作为异常判据；IQR 为 0 时该比值也未定义。

## 参数
- `partition_cols`: 分组字段，由数据集结构和分析粒度决定；示例使用 `group_date` 与 `group_id`。
- `value_col`: 数值字段名与类型；示例使用 `value`。
- `entity_id_col`: 实体标识字段；缺失值默认不纳入 `nunique()` 去重计数。
- `cat_col`: 类别字段；示例使用 `category`。
- `cond`: 布尔筛选掩码，可基于任意可用字段构造，仅用于条件计数/占比指标。
- `top_n`: 输出前 N 个类别的 N，示例取 3。
- `top_k`: 前 K 大记录值占比（TopKValueRatio）的 K，示例取 3。

## 示例

### 示例一：基础指标（数值 / 记录数 / 实体数，对应速查表 A）
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01", "2023-01-01", "2023-01-01", "2023-01-02", "2023-01-02", "2023-01-02"],
    "group_id": ["group_a", "group_a", "group_a", "group_b", "group_b", "group_b"],
    "value": [1, 2, 3, 4, 5, 6],
    "entity_id": ["entity_1", "entity_2", "entity_3", "entity_4", None, "entity_5"],
})

def calculate_basic_metrics(
    df: pd.DataFrame, partition_cols: List[str], value_col: str, entity_id_col: str
) -> List[Dict[str, Any]]:
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "value_sum": float(df[value_col].sum()),
        "record_count": int(df.shape[0]),
        "entity_count": int(df[entity_id_col].nunique()),
    }]

result = execute(
    df,
    calculate_basic_metrics,
    schema="group_date:str,group_id:str,value_sum:double,record_count:long,entity_count:long",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value", entity_id_col="entity_id"),
    partition={"by": ["group_date", "group_id"]},
)
print(result.to_markdown())
```

说明：
- 三个基础指标：求和 `df[value_col].sum()`、计数 `df.shape[0]`、去重计数 `df[entity_id_col].nunique()`。
- `entity_id` 中的缺失值会被 `nunique()` 默认忽略；group_b 有两个非缺失 ID，因此去重计数为 2。
- 列名不写死：分组键用 `partition_cols` + `**{col: df.iloc[0][col] for col in partition_cols}` 透传，指标列经 `params=dict(...)` 注入。

### 示例二：数值列分布指标（均值 / 离散 / 分位 / 形状 / 异常 / 真值 / 极值占比，对应速查表 B）
```python
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 6 + ["2023-01-02"] * 4,
    "group_id": ["group_a"] * 6 + ["group_b"] * 4,
    "value": [50000, 50000, 100, 100, 100, 1, 8, 8, 8, 2],
    "entity_id": ["entity_1", "entity_2", "entity_3", "entity_4", "entity_5", "entity_6", "entity_7", "entity_7", "entity_8", "entity_8"],
})

def numeric_distribution(
    df: pd.DataFrame,
    partition_cols: List[str],
    value_col: str,
    entity_id_col: str,
    value_threshold: float,
    top_k: int,
) -> List[Dict[str, Any]]:
    s = df[value_col]
    mean = s.mean()
    sigma = s.std()
    total = s.sum()
    quantiles = s.quantile([0.25, 0.5, 0.75])
    q25, q50, q75 = quantiles.loc[0.25], quantiles.loc[0.5], quantiles.loc[0.75]

    cond = s > value_threshold  # 真值类布尔掩码
    outlier_mask = (np.abs(s - mean) > 3 * sigma) if sigma > 0 else np.zeros(len(s), dtype=bool)

    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        # 集中 / 离散
        "mean": float(mean),
        "std": float(sigma),
        "var": float(s.var()),
        "cv": float(sigma / mean) if mean != 0 else 0.0,
        # 分位
        "q25": float(q25),
        "q50": float(q50),
        "q75": float(q75),
        "iqr": float(q75 - q25),
        # 形状
        "skew": float(s.skew()),
        "kurt": float(s.kurt()),
        "mad": float((s - mean).abs().mean()),
        # 异常
        "outlier_count": int(outlier_mask.sum()),
        "outlier_ratio": float(outlier_mask.mean()),
        # 真值类（cond 四口径）
        "true_count": int(cond.sum()),
        "true_ratio": float(cond.mean()),
        "true_value_ratio": float(df.loc[cond, value_col].sum() / total),
        "true_entity_ratio": float(df.loc[cond, entity_id_col].nunique() / df[entity_id_col].nunique()),
        # 极值占比
        "max_ratio": float(s.max() / total),
        "min_ratio": float(s.min() / total),
        "max_total_ratio": float(s.max() * (s == s.max()).sum() / total),
        "min_total_ratio": float(s.min() * (s == s.min()).sum() / total),
        "topk_value_ratio": float(s.nlargest(top_k).sum() / total),
    }]

result = execute(
    df,
    numeric_distribution,
    schema="group_date:str,group_id:str,mean:double,std:double,var:double,cv:double,q25:double,q50:double,q75:double,iqr:double,skew:double,kurt:double,mad:double,outlier_count:long,outlier_ratio:double,true_count:long,true_ratio:double,true_value_ratio:double,true_entity_ratio:double,max_ratio:double,min_ratio:double,max_total_ratio:double,min_total_ratio:double,topk_value_ratio:double",
    params=dict(partition_cols=["group_date", "group_id"], value_col="value", entity_id_col="entity_id", value_threshold=100, top_k=3),
    partition={"by": ["group_date", "group_id"]},
)
print(result.to_markdown())
```

说明：
- 一次算出 B 段全部数值列分布指标：集中/离散（mean/std/var/cv）、分位（q25/q50/q75/iqr）、形状（skew/kurt/mad）、异常（outlier_count/outlier_ratio）、真值类四口径、极值占比四指标。
- 多分位点用 `s.quantile([0.25, 0.5, 0.75])` 一次返回，再 `.loc[0.5]` 取中位数。
- `cond` 是布尔掩码，来源不限于数值——这里用 `value_threshold=100`，也可换成时间范围 / 状态类别 / 来源类别等（详见速查表 B 的「真值」四行）。
- MaxRatio 只算单条最大值（`s.max()/s.sum()`），MaxTotalRatio 把并列最大值全部计入（`s.max()*(s==s.max()).sum()/s.sum()`）；group_a 两条最大值均为 50000，两种比例约为 0.498 和 0.997。TopKValueRatio 取前 K 大记录（`top_k=3`）：group_a 前 3 条记录值之和为 50000+50000+100，占比约 0.998。它与 MaxTotalRatio 的区别是按 K 条记录计入，不要求这些值并列。
- `skew` / `kurt` 描述的是**单记录数值序列本身的形状**（右偏长尾、尖峰），不是「实体之间的集中度」。

### 示例三：类别字段集中度（众数 / 三维占比 / 熵 / top-N 名单，对应速查表 C）
```python
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from mint import execute

df = pd.DataFrame({
    "group_date": ["2023-01-01"] * 6 + ["2023-01-02"] * 4,
    "group_id": ["group_a"] * 6 + ["group_b"] * 4,
    "category": ["A", "A", "A", "A", "B", "B", "A", "A", "B", "C"],
    "value": [10, 10, 10, 10, 200, 200, 50, 50, 30, 20],
    "entity_id": ["entity_1", "entity_2", "entity_3", "entity_4", "entity_5", "entity_6", "entity_7", "entity_8", "entity_9", "entity_10"],
})

def categorical_concentration(
    df: pd.DataFrame, partition_cols: List[str], cat_col: str, value_col: str, entity_id_col: str
) -> List[Dict[str, Any]]:
    counts = df[cat_col].value_counts()
    total = counts.sum()
    n = counts.size
    p = counts / total
    entropy = float(-(p * np.log(p)).sum())
    norm_entropy = float(entropy / np.log(n)) if n > 1 else 0.0

    mode_cat = counts.index[0]
    top3_cats = counts.head(3).index
    mode_mask = df[cat_col] == mode_cat
    top3_mask = df[cat_col].isin(top3_cats)

    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        # 记录数维度
        "mode": mode_cat,
        "mode_ratio": float(counts.iloc[0] / total),
        "top3_ratio": float(counts.head(3).sum() / total),
        # 数值维度
        "mode_value_ratio": float(df.loc[mode_mask, value_col].sum() / df[value_col].sum()),
        "top3_value_ratio": float(df.loc[top3_mask, value_col].sum() / df[value_col].sum()),
        # 实体数维度
        "mode_entity_ratio": float(df.loc[mode_mask, entity_id_col].nunique() / df[entity_id_col].nunique()),
        "top3_entity_ratio": float(df.loc[top3_mask, entity_id_col].nunique() / df[entity_id_col].nunique()),
        # 均匀度
        "entropy": entropy,
        "normalized_entropy": norm_entropy,
        # 主导名单
        "top3_names": "|".join(top3_cats),
    }]

result = execute(
    df,
    categorical_concentration,
    schema="group_date:str,group_id:str,mode:str,mode_ratio:double,top3_ratio:double,mode_value_ratio:double,top3_value_ratio:double,mode_entity_ratio:double,top3_entity_ratio:double,entropy:double,normalized_entropy:double,top3_names:str",
    params=dict(partition_cols=["group_date", "group_id"], cat_col="category", value_col="value", entity_id_col="entity_id"),
    partition={"by": ["group_date", "group_id"]},
)
print(result.to_markdown())
```

说明：
- 对 `category` 这类类别字段，用 `value_counts()` 得到频数分布，再算众数 / 三维占比 / 熵 / top-N。
- 三维占比：记录数维度（`mode_ratio` / `top3_ratio`）、数值维度（`mode_value_ratio` / `top3_value_ratio`）、实体数维度（`mode_entity_ratio` / `top3_entity_ratio`）。group_a 里 A 类记录数占 4/6，但数值只占 40/440≈0.09；B 类的数值占 400/440≈0.91，说明频数占比与数值占比不必一致。
- 「top 数值占比」用 `df[cat_col].isin(top3_cats)` 命中前 N 个类别再对数值求和；实体数占比同理用 `.nunique()`。
- 若同一 `entity_id` 可以出现在多个类别中，各类别的 distinct-entity 集合可能重叠；对应占比的分子可能重复计算实体，结果不保证不超过 1。
- 任意类别字段都可作为 `cat_col`；若一个类别对应多条记录且要比较类别累计值，先 `groupby(cat_col)[value_col].sum()` 再算占比。

## 实现注意事项
- 聚合/统计结果结构通常与输入不同，**必须写显式 schema**，不要用 `schema="*"`。
- 结果统一用 `result = execute(...)` 命名，业务参数经 `params=dict(...)` 传入（`partition_cols` / `value_col` / `entity_id_col` / `cat_col` 等）。
- 函数内**不要写死列名**：一律用 `df[value_col]` / `df[entity_id_col]` / `df[cat_col]`，具体字段名经 `params=dict(...)` 注入；示例数据里的真实字段名（`value` / `entity_id` / `category`）只作为 `params` 的取值。
- 分组键用 `partition_cols: List[str]` 参数接收，返回字典里用 `**{col: df.iloc[0][col] for col in partition_cols}` 透传分组值（与 execute-metrics 对齐），不要写死 `"group_id": df.iloc[0]["group_id"]`。
- 多分位点用 `s.quantile([0.25, 0.5, 0.75])` 一次算出，返回 Series，用 `.loc[0.5]` 取值；不要逐个 `quantile(0.25)` 调三次。
- 「满足某条件」类指标先建布尔掩码 `cond`，再按需计算记录数 `cond.sum()`、记录数占比 `cond.mean()`、数值占比 `df.loc[cond, value_col].sum() / df[value_col].sum()`、实体数占比 `df.loc[cond, entity_id_col].nunique() / df[entity_id_col].nunique()`。例如 `cond = s > s.mean()` 可用于计算高于均值的记录数、占比或数值合计。
- 按组聚合时使用 `partition={"by": partition_cols}`；整体计算可省略 partition。
- 示例中的比例计算假设相应分母非零；数值和为 0 时，基于数值总和的比例未定义，实体数为 0 时实体占比未定义。含正负值时，这些数值和比例可能不在通常的非负占比范围内，应按数据定义解释。
- 示例将 `mean == 0` 时的 Cv 设为 `0.0`，这是显式的示例回退值，不是 Cv 的通用定义；若需保留未定义状态，可按调用方约定输出空值或采用其他处理。
- 计算实体相关指标前，先 `groupby(entity_id_col)[value_col].sum()` 得到「每实体累计数值」，再对这批数值算占比 / 熵 / `Cv` / top-N。
- `nunique()` 默认忽略缺失实体标识；若分析需要将缺失值作为一个类别，应明确采用不同的缺失值处理口径。
- `value_counts()` 默认忽略缺失类别；空类别序列没有众数，熵也未定义，应在计算前决定缺失值处理策略。
- 所有示例假设分区非空；空分区没有数值统计量、类别众数或有效比例，需在调用前定义空输入的返回口径。
- 标准熵范围为 0~1：0 表示频数集中于一个类别，1 表示类别频数完全均匀。它基于频数分布，不是数值总和的均匀度。
- `skew` / `kurt` 描述数值序列的形状，不等同于类别或实体之间的集中度；选择与分析问题相匹配的指标。
- 偏度、峰度、变异系数和熵等指标受样本量、零值、缺失值及分布影响；将阈值视为数据集相关的描述性参考，而非普遍规则。
- 数值示例中的 `|s - mean| > 3 * std` 只是一个明确指定的示例筛选规则，不是适用于所有分布的离群值定义。
- 不要杜撰不存在的 API；本模式涉及的 pandas/numpy 方法（`sum`/`nunique`/`quantile`/`skew`/`kurt`/`value_counts`/`np.log`/`np.abs`）均为标准方法。
