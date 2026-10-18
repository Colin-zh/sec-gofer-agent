---
id: execute-spark
title: Basic mint.execute Spark Pattern
resource_type: pattern_source
summary: Spark DataFrame 输入时的执行路径：Mint 自动推断 Spark 引擎，结果用 show() 查看，schema/params/partition 写法与本地一致。仅在需要 Spark 时选用，否则回 execute-basic。
tags: [execute, spark, dataframe]
related_source: [execute-basic, execute-partition, execute-schema, execute-yield]
priority: 70
---

## 适用场景
当用户输入是 Spark DataFrame，或者希望 `mint.execute(...)` 走 Spark 执行路径时，使用此模式。典型场景包括：
- 已经有 `SparkSession` 和 Spark DataFrame。
- 需要对 Spark DataFrame 复用同样的 execute-call 写法。
- 希望解释 Spark 场景下输出该如何查看和验证。

## 模式描述
`mint.execute(...)` 可以直接接收 Spark DataFrame。

在这种情况下：
- Mint 会根据输入对象推断 Spark 执行路径。
- 输出通常也保持 Spark DataFrame 语义。
- 调试输出时更适合使用 `show()`，而不是 `to_markdown()`。

也就是说，Spark 模式的重点不是换一套 API，而是：
- 输入对象变成 Spark DataFrame
- 输出查看方式更偏 Spark 风格
- `schema`、`params`、`partition` 的写法仍与普通 execute-call 一致

## 参数
- `df`: Spark DataFrame，一般由 `spark.createDataFrame(...)` 创建。
- `using`: 处理函数。函数签名仍应保持清晰、可执行。
- `schema`: 输出结构说明；`schema="*"` 保留输入现有列，新增列时用 `schema="*,new_col:type"` 或完整输出 schema。
- `params`: 额外参数，仍通过 `params=dict(...)` 传入。
- `partition`: Spark 下按键分组执行时使用。

## 示例

### 示例代码一: Spark DataFrame 上的基础映射替换
```python
from typing import Dict

import pandas as pd
from pyspark.sql import SparkSession

from mint import execute

input_df = pd.DataFrame({"entity_id": [0, 1, 2], "value": ["a", "b", "c"]})
map_dict = {"a": "alpha", "b": "beta", "c": "gamma"}

spark = SparkSession.builder.getOrCreate()
sdf = spark.createDataFrame(input_df)

def map_value(df: pd.DataFrame, mapping: Dict[str, str]) -> pd.DataFrame:
    df = df.copy()
    df["value"] = df["value"].map(mapping)
    return df

result = execute(
    sdf,
    map_value,
    schema="*",
    params=dict(mapping=map_dict),
    engine_conf={"mint.spark.use_pandas_udf": True},
)

print(type(result))
# Output: <class 'pyspark.sql.classic.dataframe.DataFrame'>

result.show()
"""
+---------+-----+
|entity_id|value|
+---------+-----+
|        0|alpha|
|        1| beta|
|        2|gamma|
+---------+-----+
"""
```

说明：
- 这里输入是 `sdf`，所以执行会走 Spark 路径。
- 输出通常是 Spark DataFrame，更适合用 `show()` 查看。

### 示例代码二: Spark DataFrame + 分组聚合输出
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
    engine_conf={"mint.spark.use_pandas_udf": True},
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

说明：
- `partition={"by": "category"}` 表示按 `category` 分组执行。
- 因为输出列结构与输入完全不同，所以这里必须写显式 schema。

### 示例代码三: Spark DataFrame + partition + presort 生成分组内 row_number
```python
import pandas as pd
from pyspark.sql import SparkSession

from mint import execute

pdf = pd.DataFrame(
    {
        "entity_id": ["e1", "e1", "e1", "e2", "e2"],
        "event_time": [3, 1, 2, 2, 1],
        "value": [30, 10, 20, 22, 11],
    }
)

spark = SparkSession.builder.getOrCreate()
sdf = spark.createDataFrame(pdf)

def add_row_number(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().reset_index(drop=True)
    df["row_number"] = range(1, len(df) + 1)
    return df

result = execute(
    sdf,
    add_row_number,
    schema="entity_id:str,event_time:long,value:long,row_number:long",
    partition={"by": "entity_id", "presort": "event_time asc"},
    engine_conf={"mint.spark.use_pandas_udf": True},
)

result.orderBy("entity_id", "row_number").show()
"""
+---------+----------+-----+----------+
|entity_id|event_time|value|row_number|
+---------+----------+-----+----------+
|       e1|         1|   10|         1|
|       e1|         2|   20|         2|
|       e1|         3|   30|         3|
|       e2|         1|   11|         1|
|       e2|         2|   22|         2|
+---------+----------+-----+----------+
"""
```

说明：
- `partition={"by": "entity_id", "presort": "event_time asc"}` 表示先按 `entity_id` 分组，再在每个分组内按 `event_time` 升序排序。
- 函数拿到的 `df` 已经是“分组内按 presort 排好序”的局部数据，所以可以直接用当前位置生成 `row_number`。
- 这不是像 `yield` 示例那样做截断，而是保留每一行，并补出一个分组内顺序号列。
- 因为输出新增了 `row_number`，所以这里必须写显式 schema。

## 示例形态
- `execute(spark_df, using=func, schema="*", params=dict(...), engine_conf={"mint.spark.use_pandas_udf": True})`
- `execute(spark_df, using=func, schema="category:str,max:long,min:long", partition={"by": "category"}, engine_conf={"mint.spark.use_pandas_udf": True})`
- `execute(spark_df, using=func, schema="entity_id:str,event_time:long,value:long,row_number:long", partition={"by": "entity_id", "presort": "event_time asc"}, engine_conf={"mint.spark.use_pandas_udf": True})`

## 生成注意事项
- `execute-spark` 仅在需要 Spark 执行路径时作为主参考；
    普通本地 pandas 场景优先参考 `execute-basic`。
- Spark 模式下仍然优先生成普通 Python callable，不要为了 Spark 单独发明一套新入口。
- 如果输入是 Spark DataFrame，文档示例应优先使用 `show()` 展示结果，而不是 `to_markdown()`。
- `schema="*"` 只保留输入现有列，适用于 shape-preserving 场景；新增列时使用 `schema="*,new_col:type"` 或完整输出 schema，聚合、重塑结果时必须声明完整输出结构。
- 如果示例里用了 `partition`，就要说明是“按组执行”，而不是全表执行。
- 如果同时用了 `presort`，要明确说明函数拿到的是“分组内已排序”的数据，这非常适合实现 `row_number`、累计值、前后序比较等顺序敏感逻辑。
- `engine_conf={"mint.spark.use_pandas_udf": True}` 是 Spark 模式下的推荐配置，能让 Spark 执行更高效。
- 不要把 “Spark 输入” 和 “Spark SQL 语法” 混为一谈；这里描述的是 Spark DataFrame 执行路径。
- 与其他 pattern source 一致，统一使用 `result = execute(...)` 作为执行结果变量命名。
