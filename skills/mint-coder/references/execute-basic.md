---
id: execute-basic
title: Basic mint.execute Pattern
resource_type: pattern_source
summary: Pandas 输入的 mint.execute 基础调用模式：无分组的单步转换；schema="*" 保留输入列，新增列用 schema="*,new_col:type" 或完整输出 schema。需分组请参考 execute-partition，需组内筛选请参考 execute-yield。
tags: [execute, basic, schema, params]
related_source: [execute-partition, execute-schema, execute-yield, execute-map-single, execute-map-multi, execute-spark]
priority: 80
---

## 适用场景
当用户希望基于现有 DataFrame 做一次单步转换，并将逻辑封装成可传给 `mint.execute(...)` 的 Python callable 时，优先参考此模式。

典型场景包括：
- 对某个字段做映射、清洗、标准化。
- 基于额外参数做值替换或补充列计算。
- 输出结构与输入结构一致，或只有少量、明确的 schema 变化。

## 模式描述
`mint.execute(...)` 是 Mint 最轻量的执行入口。最基础的调用形态通常包含四个元素：
- `df`: 输入数据，可以是 pandas DataFrame、Spark DataFrame，或 Mint 支持的其他 dataframe-like 对象。
- `using`: 真正执行转换逻辑的 callable。
- `schema`: 输出 schema 说明。`schema="*"` 保留输入的所有现有列；新增列时用 `schema="*,new_col:type"` 或列出完整输出 schema。
- `params`: 额外处理参数，通过 `params=dict(...)` 传给 callable。

推荐先写一个签名清晰的 Python 函数，再将它传给 `execute(...)`，而不是在外层拼复杂表达式。

## 参数
- `df`: 输入数据框对象。第一版说明文档优先以 pandas / Spark DataFrame 作为示例。
- `using`: 处理函数。第一个参数通常是当前批次或当前分区的数据。
- `schema`: 输出结构说明。结构不变时用 `*`，结构变化时写显式 schema。
- `params`: 额外处理参数。函数里需要的映射字典、阈值、字段名等都应通过这里传入。
- `partition`: 仅在按键分组执行时使用；不需要分组时不要额外添加。
- `engine`: 一般不必显式指定；如果输入本身是 Spark DataFrame，Mint 会推断 Spark 执行路径。

## 示例

### 示例代码一: shape-preserving 映射替换
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

print(result.to_markdown())
"""
|    |   entity_id | value   |
|---:|------------:|:--------|
|  0 |           0 | alpha   |
|  1 |           1 | beta    |
|  2 |           2 | gamma   |
"""
```

### 示例代码二: 带额外参数的基础 execute 调用
```python
import pandas as pd
from mint import execute

input_df = pd.DataFrame(
    {"entity_id": [0, 1, 2], "value": [12, 18, 24]}
)

def add_category(df: pd.DataFrame, threshold: int) -> pd.DataFrame:
    df = df.copy()
    df["category"] = df["value"].apply(lambda x: "high" if x >= threshold else "low")
    return df

result = execute(
    input_df,
    add_category,
    schema="*,category:str",
    params=dict(threshold=18),
)

print(result.to_markdown())
"""
|    |   entity_id |   value | category   |
|---:|------------:|--------:|:-----------|
|  0 |           0 |      12 | low        |
|  1 |           1 |      18 | high       |
|  2 |           2 |      24 | high       |
"""
```

## 示例形态
- `execute(df, using=func, schema="*", params=dict(...))`
- `execute(df, using=func, schema="entity_id:long,value:str", params=dict(...))`

## 生成注意事项
- 优先生成签名清晰的普通 Python 函数，再传给 `mint.execute(...)`。
- `schema="*"` 只保留输入已有列；新增列时使用 `schema="*,new_col:type"` 或完整 schema，删列、改列名时列出完整输出 schema。
- 处理参数走 `params`
- 不要编造新的运行入口、包装器或仓库中不存在的 API
- 如果输入是 Spark DataFrame，输出通常也会保持 Spark DataFrame 语义
- 保持第一版草稿最小可用
- 统一使用 `result = execute(...)` 作为执行结果变量命名。
- 文档中的 `input_df = ...` 造数示例仅用于模式演示；在 coder-cli 可执行草稿中，
    输入应改为 `df = pd.read_parquet(DATASET_PATH)` 并使用该 `df` 调用 `execute(...)`。
