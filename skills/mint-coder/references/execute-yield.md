---
id: execute-yield
title: Basic mint.execute Yield Pattern
resource_type: pattern_source
summary: 通过 Iterable/yield 逐条产出结果的 execute 模式，适合组内截断、顺序筛选与流式产出（非聚合）。配合 partition+presort 做组内 top-N、首条/末条。
tags: [execute, yield, iterable]
related_source: [execute-partition, execute-basic]
priority: 80
---

## 适用场景
当用户希望函数“逐条产出结果”而不是一次性返回完整 DataFrame 或 `list[dict]` 时，使用此模式。典型场景包括：
- 组内取 top N
- 在排好序的数据上按条件逐条筛选
- 需要保留原行结构，但只输出部分记录

## 模式描述
在 `mint.execute(...)` 中，`using` 函数不一定要返回 DataFrame。
它也可以返回：
- `Iterable[dict[str, Any]]`
- 其他可逐条迭代的记录结构

如果配合 `partition + presort` 使用，函数拿到的就是“每个分组内已经排好序的数据”，这时 `yield` 非常适合做：
- top N
- 首条 / 末条
- 组内逐条截断

## 参数
### mint.execute 函数参数
- `df`: (必须) 输入数据框
- `using`: (必须) 返回 iterable 的函数
- `schema`: (必须) 输出 schema 说明
- `partition`: (通常需要) 分组和排序配置

### 自定义函数返回值
- `Iterable[Dict[str, Any]]`: 最常见的 yield 写法
- 每次 `yield` 一条记录，最终由 Mint 组装成结果数据框

## 示例
### 示例代码一: 分组内按 presort 取 top 2
```python
from typing import Any, Dict, Iterable, List

import pandas as pd
from pyspark.sql import SparkSession

from mint import execute

df = pd.DataFrame(
    {
        "entity_id": ["e1", "e1", "e1", "e2", "e2", "e2"],
        "event_time": [1, 2, 3, 1, 2, 3],
        "value": [10, 20, 30, 40, 50, 60],
    }
)

spark = SparkSession.builder.getOrCreate()
sdf = spark.createDataFrame(df)

def top_two(df: List[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    n = 0
    while n < 2:
        yield df[n]
        n = n + 1

result = execute(
    df=sdf,
    using=top_two,
    schema="*",
    partition={"by": "entity_id", "presort": "event_time desc"},
)

result.show()
"""
+---------+----------+-----+
|entity_id|event_time|value|
+---------+----------+-----+
|       e1|         3|   30|
|       e1|         2|   20|
|       e2|         3|   60|
|       e2|         2|   50|
+---------+----------+-----+
"""
```

### 示例代码二: 分组内按 presort 只保留第一条
```python
from typing import Any, Dict, Iterable, List

import pandas as pd
from mint import execute

input_df = pd.DataFrame(
    {
        "entity_id": ["e1", "e1", "e1", "e2", "e2"],
        "event_time": [3, 1, 2, 2, 1],
        "value": [30, 10, 20, 22, 11],
    }
)

def first_row(df: List[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    if len(df) > 0:
        yield df[0]

result = execute(
    input_df,
    first_row,
    schema="*",
    partition={"by": "entity_id", "presort": "event_time asc"},
)

print(result.sort_values(["entity_id", "event_time"]).to_markdown())
"""
|    | entity_id   |   event_time |   value |
|---:|:------------|-------------:|--------:|
|  0 | e1          |            1 |      10 |
|  1 | e2          |            1 |      11 |
"""
```

## 生成注意事项
- `yield` 模式更适合“保留部分记录”或“逐条产出”的场景，而不是常规整表聚合。
- 如果返回的每条记录结构与输入一致，可以考虑 `schema="*"`；它只包含输入已有列。若产出的记录包含新增列，使用 `schema="*,new_col:type"` 或显式列出完整输出 schema。
- 配合 `presort` 使用时，要明确说明函数拿到的数据已经按指定顺序排好。
- 如果函数签名使用的是 `List[Dict[str, Any]]`，通常是在消费“组内已排序的记录列表”。
- `yield` 适合 top N、first row、规则命中的逐条输出；如果只是生成一个聚合结果，优先考虑直接返回 `list[dict]`。
