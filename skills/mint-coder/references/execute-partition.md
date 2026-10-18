---
id: execute-partition
title: Basic mint.execute Partition Pattern
resource_type: pattern_source
summary: 按字段分组执行 mint.execute：函数拿到组内局部数据，可 presort 组内排序，适合分组聚合与组内计算。若只需保留部分记录或截断，请用 execute-yield。
tags: [execute, partition, group]
related_source: [execute-basic, execute-schema, execute-yield]
priority: 85
---

## 适用场景
当用户希望“按某个字段或多个字段分组后再执行逻辑”时，使用此模式。典型场景包括：
- 按 `entity_id`、`category` 或其他字段分组聚合。
- 每个分组输出一行或多行结果。
- 组内做局部统计、窗口前置计算或顺序敏感处理。

## 模式描述
`partition` 用于告诉 `mint.execute(...)`：
- 先按哪些字段拆分数据
- 是否需要在每个分组内先排序

最常见的写法：
- `partition={"by": "entity_id"}`
- `partition={"by": ["entity_id", "category"]}`
- `partition={"by": "entity_id", "presort": "event_time desc"}`

函数拿到的 `df` 不再是全表，而是每个分组内部的局部数据。

## 参数
### mint.execute 函数参数
- `df`: (必须) 输入数据框
- `using`: (必须) 自定义函数
- `schema`: (必须) 输出 schema 说明
- `partition`: (必须) 分组配置，至少要包含 `by`

### partition 配置参数
- `by`: (必须) 分组字段，可以是单个字段名或字段列表
- `presort`: (可选) 分组内排序表达式，例如 `"event_time desc"`、`"value asc"`

## 示例
### 示例代码一: 每个分组输出一行聚合结果
```python
from typing import Any, Dict, List

import pandas as pd
from pyspark.sql import SparkSession

from mint import execute

df = pd.DataFrame(
    {"category": ["alpha", "alpha", "alpha", "beta", "beta", "beta"],
     "value": [1, 2, 3, 4, 5, 6]}
)

spark = SparkSession.builder.getOrCreate()
sdf = spark.createDataFrame(df)

def min_max(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return [
        {
            "category": df.iloc[0]["category"],
            "max": df["value"].max(),
            "min": df["value"].min(),
        }
    ]

result = execute(
    sdf,
    min_max,
    schema="category:str,max:long,min:long",
    partition={"by": "category"},
)

result.show()
"""
+--------+---+---+
|category|max|min|
+--------+---+---+
|   alpha|  3|  1|
|    beta|  6|  4|
+--------+---+---+
"""
```

### 示例代码二: 按多个字段分组计算组内记录数
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

input_df = pd.DataFrame(
    {
        "entity_id": ["e1", "e1", "e1", "e2", "e2"],
        "event_time": ["2026-08-01", "2026-08-01", "2026-08-02", "2026-08-01", "2026-08-01"],
        "value": [10, 20, 30, 40, 50],
    }
)

def count_rows(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return [
        {
            "entity_id": df.iloc[0]["entity_id"],
            "event_time": df.iloc[0]["event_time"],
            "row_count": len(df),
        }
    ]

result = execute(
    input_df,
    count_rows,
    schema="entity_id:str,event_time:str,row_count:long",
    partition={"by": ["entity_id", "event_time"]},
)

print(result.to_markdown())
"""
|    | entity_id   | event_time   |   row_count |
|---:|:------------|:-------------|------------:|
|  0 | e1          | 2026-08-01   |           2 |
|  1 | e1          | 2026-08-02   |           1 |
|  2 | e2          | 2026-08-01   |           2 |
"""
```

## 生成注意事项
- 使用 `partition` 时，要明确函数处理的是“单个分组”的局部数据，而不是整个表。
- 如果每个分组输出结构与输入不同，必须写显式 schema。
- 如果分组内逻辑依赖顺序，再加 `presort`，不要假设原始顺序天然可靠。
- `partition` 适合聚合、组内排序、分组编号、组内规则判断等场景。
- 如果函数要输出多行结果，可以返回 `pd.DataFrame`、`list[dict]` 或 `Iterable[dict]`，但要确保与 schema 对齐。
