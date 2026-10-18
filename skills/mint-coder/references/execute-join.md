---
id: execute-join
title: Basic mint join Pattern
resource_type: pattern_source
summary: 多表连接模式：用 mint.backends.api.join 按公共键合并两张或多张表，pandas 与 Spark 输入都支持。how 覆盖 inner/left_outer/right_outer/full_outer/cross/semi/anti；mint.execute 本身只支持单表输入，多表合并要走 join。
tags: [join, multi-table, pandas, spark]
related_source: [execute-basic, execute-spark, execute-schema]
priority: 75
---

## 适用场景
当用户需要把两张或多张表按某个键合并成一张时，使用此模式。典型场景包括：
- 两张表有公共键，想按左表（或右表、全量）对齐并带上对方的字段。
- 只想保留左表"有匹配"或"无匹配"的行（半连接 / 反连接）。
- 已经在 Spark 场景下，想把两个 Spark DataFrame 合并。

注意：`mint.execute(...)` 只接受**单表输入**（一个 `df` + 一个 `using` 函数），多表合并不能走 `execute`，而要走 `mint.backends.api.join`（本模式），或更高层的 `MintWorkflow.join` 搭 DAG。

## 模式描述
多表合并的核心入口是 `mint.backends.api.join`：

```python
from mint.backends import api as ma

result = ma.join(df1, df2, how="left_outer", on=["entity_id"])
```

要点：
- 接受 `df1, df2, *dfs`，可以一次合并两张以上的表。
- `how` 决定连接语义（见下方"how 可选项"）。
- `on` 可以省略，Mint 会自动取两张表的所有公共列作为连接键；显式传入时会校验。
- 输入是 pandas DataFrame 时，返回 pandas DataFrame（用 `to_markdown()` 查看）。
- 输入是 Spark DataFrame 时，返回 Spark DataFrame（用 `show()` 查看），且需要显式指定 Spark 引擎。

## 参数
### mint.backends.api.join 参数
- `df1`: (必须) 左表。
- `df2`: (必须) 右表。
- `*dfs`: (可选) 更多表，按同样 `how` 依次连接。
- `how`: (必须) 连接方式，见下表。
- `on`: (可选) 连接键列表，例如 `["entity_id"]`；省略时自动推断为所有公共列。
- `engine`: (可选) 执行引擎。Spark 输入时传 `SparkExecutionEngine(spark)`，不能直接传裸 `SparkSession`。
- `engine_conf`: (可选) 引擎配置，Spark 下推荐 `{"mint.spark.use_pandas_udf": True}`。
- `as_mint` / `as_local`: (可选) 控制返回类型，默认按输入类型返回原生数据框。

### how 可选项
| `how` | 语义 | pandas 等价 | Spark 等价 |
| --- | --- | --- | --- |
| `inner` | 内连接，只保留两侧都匹配的行 | `how="inner"` | `"inner"` |
| `left_outer` | 左外连接，保留左表全部行，右表无匹配补 null | `how="left"` | `"left_outer"` |
| `right_outer` | 右外连接，保留右表全部行 | `how="right"` | `"right_outer"` |
| `full_outer` | 全外连接，保留两侧全部行 | `how="outer"` | `"full_outer"` |
| `cross` | 笛卡尔积，要求两表**无公共列** | 无直接等价 | `"cross"` |
| `semi` / `left_semi` | 左半连接，只保留左表有匹配的行，只输出左表列 | `df[df.key.isin(right.key)]` | `"left_semi"` |
| `anti` / `left_anti` | 左反连接，只保留左表无匹配的行，只输出左表列 | `df[~df.key.isin(right.key)]` | `"left_anti"` |

说明：
- `semi` 与 `left_semi` 等价，`anti` 与 `left_anti` 等价。
- 没有 `left` / `outer` 这两个值（pandas/Spark 里的 `left`、`outer` 要分别写成 `left_outer`、`full_outer`）。
- 每个 `how` 都有对应的便捷封装：`inner_join`、`left_outer_join`、`right_outer_join`、`full_outer_join`、`cross_join`、`semi_join`、`anti_join`，它们只是把 `how` 固定住。

## 示例

### 示例代码一: pandas 左外连接（自动推断连接键）
```python
import pandas as pd

from mint.backends import api as ma

records_df = pd.DataFrame(
    {"entity_id": [1, 2, 3], "category": ["alpha", "beta", "gamma"]}
)
events_df = pd.DataFrame(
    {
        "entity_id": [1, 1, 2, 2],
        "related_id": [101, 102, 103, 104],
        "event_time": [10, 20, 30, 40],
        "value": [100, 200, 50, 80],
    }
)

result = ma.join(records_df, events_df, how="left_outer")

print(result.to_markdown())
"""
|    |   entity_id | category   |   related_id |   event_time |   value |
|---:|------------:|:-----------|-------------:|-------------:|--------:|
|  0 |           1 | alpha      |          101 |           10 |     100 |
|  1 |           1 | alpha      |          102 |           20 |     200 |
|  2 |           2 | beta       |          103 |           30 |      50 |
|  3 |           2 | beta       |          104 |           40 |      80 |
|  4 |           3 | gamma      |         <NA> |         <NA> |    <NA> |
"""
```

说明：
- 没传 `on`，Mint 自动把公共列 `entity_id` 作为连接键。
- `left_outer` 保留左表全部 3 条记录；`entity_id=3` 在右表无匹配，右表字段补 `null`。

### 示例代码二: 内连接 / 半连接 / 反连接的区别
```python
import pandas as pd

from mint.backends import api as ma

records_df = pd.DataFrame(
    {"entity_id": [1, 2, 3], "category": ["alpha", "beta", "gamma"]}
)
events_df = pd.DataFrame(
    {
        "entity_id": [1, 1, 2, 2],
        "related_id": [101, 102, 103, 104],
        "event_time": [10, 20, 30, 40],
        "value": [100, 200, 50, 80],
    }
)

inner = ma.inner_join(records_df, events_df)   # 只保留有匹配的行（entity_id 1、2）
semi = ma.semi_join(records_df, events_df)     # 只保留左表有匹配的行，且只输出左表列
anti = ma.anti_join(records_df, events_df)     # 只保留左表无匹配的行，且只输出左表列

print(inner.to_markdown())
"""
|    |   entity_id | category   |   related_id |   event_time |   value |
|---:|------------:|:-----------|-------------:|-------------:|--------:|
|  0 |           1 | alpha      |          101 |           10 |     100 |
|  1 |           1 | alpha      |          102 |           20 |     200 |
|  2 |           2 | beta       |          103 |           30 |      50 |
|  3 |           2 | beta       |          104 |           40 |      80 |
"""

print(anti.to_markdown())
"""
|    |   entity_id | category   |
|---:|------------:|:-----------|
|  0 |           3 | gamma      |
"""
```

说明：
- `inner` 保留两侧都匹配的行，并带上两边的列。
- `semi` / `anti` 只输出**左表**的列：`semi` 是"有匹配"、`anti` 是"无匹配"，本质上是拿右表当过滤条件。

### 示例代码三: Spark 左外连接
```python
import pandas as pd
from pyspark.sql import SparkSession

from mint.backends import SparkExecutionEngine
from mint.backends import api as ma

records_df = pd.DataFrame(
    {"entity_id": [1, 2, 3], "category": ["alpha", "beta", "gamma"]}
)
events_df = pd.DataFrame(
    {
        "entity_id": [1, 1, 2, 2],
        "related_id": [101, 102, 103, 104],
        "event_time": [10, 20, 30, 40],
        "value": [100, 200, 50, 80],
    }
)

spark = SparkSession.builder.getOrCreate()
records_sdf = spark.createDataFrame(records_df)
events_sdf = spark.createDataFrame(events_df)

result = ma.join(
    records_sdf,
    events_sdf,
    how="left_outer",
    on=["entity_id"],
    engine=SparkExecutionEngine(spark),
    engine_conf={"mint.spark.use_pandas_udf": True},
)

result.show()
"""
+---------+----------+----------+----------+-----+
|entity_id| category |related_id|event_time|value|
+---------+----------+----------+----------+-----+
|        1|     alpha|       101|        10|  100|
|        1|     alpha|       102|        20|  200|
|        2|      beta|       103|        30|   50|
|        2|      beta|       104|        40|   80|
|        3|     gamma|      NULL|      NULL| NULL|
+---------+----------+----------+----------+-----+
"""
```

说明：
- 输入是 Spark DataFrame，`engine` 要传 `SparkExecutionEngine(spark)`，不能直接传裸 `spark`。
- 返回的是原生 Spark DataFrame，所以用 `show()` 查看，而不是 `to_markdown()`。

## 示例形态
- `join(df1, df2, how="left_outer")` —— 自动推断公共键。
- `join(df1, df2, how="left_outer", on=["entity_id"])` —— 显式指定连接键。
- `join(df1, df2, df3, how="inner", on=["entity_id"])` —— 三张表依次 inner join。
- `join(sdf1, sdf2, how="left_outer", on=["entity_id"], engine=SparkExecutionEngine(spark), engine_conf={"mint.spark.use_pandas_udf": True})` —— Spark 场景。

## 生成注意事项
- 连接前确认输入粒度与预期基数，显式检查富化表键唯一性、连接后事件数/数值合计和未匹配率。连接膨胀、有效期关联与未来信息泄漏见 [关联契约](relational-contracts.md)；不能任意去重修复未知的一对多关系。
- `mint.execute(...)` 只支持单表输入，多表合并必须用 `mint.backends.api.join`（或 `MintWorkflow.join`），不要试图把多表塞进 `execute` 的 `df` 参数。
- `how` 只有 `inner / left_outer / right_outer / full_outer / cross / semi / left_semi / anti / left_anti`；pandas/Spark 里的 `left`、`outer` 不是合法值，要分别写成 `left_outer`、`full_outer`。
- `cross`（笛卡尔积）要求两表**没有公共列**，否则会抛 `SchemaError: invalid cross join`；有公共列的场景不要用 `cross_join`。
- `on` 可省略，Mint 会自动取所有公共列；如果两张表有多个公共列，它们会一起作为连接键。显式传 `on` 时会校验是否合法。
- `semi` / `anti` 只输出左表列，不要把右表的列写进预期结果里。
- Spark 场景：`engine` 必须传 `SparkExecutionEngine(spark)`（裸 `SparkSession` 未注册，会报 `MintPluginsRegistrationError`）；并确保 worker 与 driver 的 Python 一致（必要时设 `os.environ["PYSPARK_PYTHON"] = sys.executable`）。
- 输入是 pandas 时返回 pandas DataFrame（`to_markdown()` 查看），输入是 Spark 时返回 Spark DataFrame（`show()` 查看）。
- 统一使用 `result = join(...)` 作为执行结果变量命名。
- 文档中的 `records_df = ...` / `events_df = ...` 造数仅用于模式演示；在 coder-cli 可执行草稿中，多表输入应改为从多个数据源读入（例如 `df1 = pd.read_parquet(DATASET_PATH_1)`、`df2 = pd.read_parquet(DATASET_PATH_2)`），再调用 `join(...)`。
