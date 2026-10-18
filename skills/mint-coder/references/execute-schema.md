---
id: execute-schema
title: Basic mint.execute Schema Pattern
resource_type: pattern_source
summary: mint.execute 输出 schema 的写法与类型：schema="*" 保留输入列；新增列用 schema="*,new_col:type" 或完整 schema，并声明对应列类型（如 str/int/long/double、list/struct）。
tags: [execute, schema, types]
related_source: [execute-basic, execute-partition, execute-spark, execute-yield]
priority: 85
---

## 适用场景
当用户需要在 `mint.execute(...)` 中明确说明输出结构时，使用此模式。典型场景包括：
- 判断当前场景是否可以使用 `schema="*"`。
- 新增列、删列、改列名后，补齐显式 schema。
- 解释 Mint schema 表达式支持哪些类型。
- 需要给聚合结果、嵌套结构结果定义输出格式。

## 模式描述
`schema` 用于描述 `execute(...)` 的输出结构。

最常见的两种写法：
- `schema="*"`：保留输入的所有现有列，适合 shape-preserving 场景；它不会包含 callable 新增的列。
- `schema="*,new_col:type"`：保留输入的所有现有列，并声明一个新增输出列及其类型。
- `schema="entity_id:type1,category:type2"`：显式声明输出列和类型，适合输出结构发生变化的场景。

核心判断标准：
- 如果只是改值、不改列结构，可以优先考虑 `schema="*"`。
- 如果新增列，使用 `schema="*,new_col:type"` 或显式列出完整输出 schema；不能只写 `schema="*"`。
- 如果删列、改列名或以其他方式改变输出结构，就应显式列出完整输出 schema。

## 支持的常见类型
Mint 的 schema 表达式底层会转换为 pyarrow schema。当前常见类型包括：

### 标量类型
- `str` / `string`
- `bool` / `boolean`
- `byte` / `int8`
- `short` / `int16`
- `int` / `int32`
- `long` / `int64`
- `ubyte` / `uint8`
- `ushort` / `uint16`
- `uint` / `uint32`
- `ulong` / `uint64`
- `float16`
- `float` / `float32`
- `double` / `float64`
- `date`
- `datetime`
- `binary` / `bytes`
- `null`

### 复合类型
- 列表类型：`[str]`、`[int]`
- 结构体类型：`{category:str,value:long}`
- Map 类型：`<str,int>`

说明：
- 当前文档中的完整可执行复合类型示例，优先使用 `list + struct` 组合。
- `map` 类型语法在 schema 表达式层是支持的，但在 `mint.execute(...)` 的常见 Python 返回对象形态下，直接落成稳定示例并不简单。
- 例如直接返回 Python `dict`，更容易被推断成 `struct`；返回 `list[tuple]` 这类键值对结构，也可能在 Arrow cast 阶段失败。

## 示例

### 示例代码一: 输出结构不变，使用 `schema="*"`
```python
from typing import Dict

import pandas as pd
from mint import execute

input_df = pd.DataFrame({"entity_id": [0, 1, 2], "value": ["a", "b", "c"]})
map_dict = {"a": "alpha", "b": "beta", "c": "gamma"}

def map_value(df: pd.DataFrame, mapping: Dict[str, str]) -> pd.DataFrame:
    df = df.copy()
    df["value"] = df["value"].map(mapping)
    return df

result = execute(
    input_df,
    map_value,
    schema="*",
    params=dict(mapping=map_dict),
)
```

说明：
- 输入列是 `entity_id,value`
- 输出列仍然是 `entity_id,value`
- 所以可以使用 `schema="*"`

### 示例代码二: 新增列并保留输入列
```python
import pandas as pd
from mint import execute

input_df = pd.DataFrame({"entity_id": [0, 1, 2], "value": ["A", "B", "C"]})

def convert_lowercase(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["value_lowercase"] = df["value"].str.lower()
    return df

result = execute(
    input_df,
    convert_lowercase,
    schema="*,value_lowercase:str",
)
```

说明：
- 输出新增了 `value_lowercase`
- `schema="*"` 只描述输入已有列，不包含 callable 新增的 `value_lowercase`
- 使用 `schema="*,value_lowercase:str"` 保留输入列并声明新增列；也可以列出完整 schema

### 示例代码三: 复合类型 schema 表达式
```python
import pandas as pd
from mint import execute

input_df = pd.DataFrame(
    {
        "entity_id": [0, 1],
        "category": ["A", "B"],
        "value": [10, 20],
    }
)

def build_nested_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["tags"] = df["category"].apply(lambda x: [x, x.lower()])
    df["meta"] = df.apply(
        lambda row: {"category": row["category"], "value": int(row["value"])},
        axis=1,
    )
    return df[["entity_id", "tags", "meta"]]

result = execute(
    input_df,
    build_nested_columns,
    schema="entity_id:long,tags:[str],meta:{category:str,value:long}",
)

print(result.to_markdown())
"""
|    |   entity_id | tags       | meta                              |
|---:|------------:|:-----------|:----------------------------------|
|  0 |           0 | ['A', 'a'] | {'category': 'A', 'value': 10}    |
|  1 |           1 | ['B', 'b'] | {'category': 'B', 'value': 20}    |
"""
```

说明：
- `tags:[str]` 表示列表列
- `meta:{category:str,value:long}` 表示结构体列
- 这类写法适合更复杂的结构化输出场景
- `struct` 内部字段顺序要与实际返回对象保持一致；如果 schema 写成 `meta:{value:long,category:str}`，但返回对象实际顺序是 `category -> value`，执行时可能报类型对齐错误

### Map 类型补充说明
```python
output_schema = "entity_id:long,meta_map:<str,long>"
```

说明：
- `map` 类型在 schema 语法层是支持的。
- 但在当前 `mint.execute(...)` 场景下，常见 Python 返回值如 `dict`、`list[tuple[str, int]]`、`list[dict[key,value]]`，都不容易直接稳定映射成 Mint 期望的 map 列。
- 因此在第一版技能文档里，更推荐优先使用：
  - `list` 类型：`[str]`
  - `struct` 类型：`{category:str,value:long}`
- 如果需要表达“一组键值信息”，当前更稳妥的方式通常是使用 `struct`，而不是强行输出 `map`。

## 示例形态
- `execute(df, using=func, schema="*")`
- `execute(df, using=func, schema="*,new_col:str")`
- `execute(df, using=func, schema="entity_id:long,value:str,new_col:str")`
- `execute(df, using=func, schema="entity_id:long,tags:[str],meta:{category:str,value:long}")`
- `execute(df, using=func, schema="entity_id:long,meta_map:<str,long>")`

## 生成注意事项
- 字段名/顺序一致不等于真实类型转换通过。nullable类型、日期、Decimal、嵌套列表和空输出需要实际Mint/Arrow结果验证，见 [分层验证](validation.md)；不可编造未验证的schema类型。
- 判断 schema 写法时，先看“输出列结构是否变化”，不要只看处理逻辑是否简单。
- `schema="*"` 只包含输入中已有的列，不能单独用于新增列。
- 新增列时使用 `schema="*,new_col:type"` 或完整输出 schema；删列、改列名时使用完整输出 schema。
- 优先使用 Mint 已支持的 schema 表达式类型，不要编造仓库中不存在的类型名。
- Python `int` 在 Mint schema 语义下常对应 `long` / `int64`，写文档时建议直接使用显式 schema 类型名。
- 如果输出结构复杂，先写 schema，再回头检查函数返回值是否与 schema 一致。
