---
id: execute-map-multi
title: Basic mint.execute Map Multi Pattern
resource_type: pattern_source
summary: 多字段映射转换模式：input_cols/output_cols 成对处理多列转换，可覆盖原列或新增多列。仅单个字段请用 execute-map-single。
tags: [execute, map, multi]
related_source: [execute-map-single, execute-schema, execute-basic]
priority: 80
---

## 适用场景
当用户希望同时对多个字段做相同或相似的映射逻辑时，使用此模式。例如：
- 同时把多个文本字段转为小写。
- 同时把多个状态字段按各自字典映射成标准值。
- 同时新增多列输出，而不是只处理一个 `input_col/output_col`。

## 模式描述
这是 `execute-map-single` 的多列版。核心思路是：
- 使用 `input_cols` 描述多个输入字段
- 使用 `output_cols` 描述多个输出字段
- 在函数内部用 `zip(input_cols, output_cols)` 逐对处理

如果是“原字段覆盖”，通常可以继续使用 `schema="*"`。
如果是“新增多个输出字段”，则应使用 `schema="*,new_col_1:type,new_col_2:type"` 或显式声明完整的输出 schema。单独的 `schema="*"` 只包含输入中已有的列。

## 参数
### mint.execute 函数参数
- `df`: (必须) 输入数据框
- `using`: (必须) 自定义函数
- `schema`: (必须) 输出 schema 说明

### 自定义函数参数
- `input_cols`: (必须) 输入字段名列表，例如 `["category", "value"]`
- `output_cols`: (必须) 输出字段名列表，例如 `["category_lower", "value_lower"]`
- `map_dicts`: (可选) 多字段映射字典，通常是 `dict[str, dict[str, str]]`

## 示例
### 示例代码一: 多个输入字段映射到多个新增输出字段
```python
from typing import Sequence

import pandas as pd
from mint import execute

input_df = pd.DataFrame(
    {
        "entity_id": [0, 1, 2],
        "category": ["ALPHA", "BETA", "GAMMA"],
        "value": ["FIRST", "SECOND", "THIRD"],
    }
)
input_cols = ["category", "value"]
output_cols = ["category_lower", "value_lower"]

print(input_df.to_markdown())
"""
|    |   entity_id | category   | value   |
|---:|------------:|:-----------|:--------|
|  0 |           0 | ALPHA      | FIRST   |
|  1 |           1 | BETA       | SECOND  |
|  2 |           2 | GAMMA      | THIRD   |
"""

def lowercase_multi(
    df: pd.DataFrame,
    input_cols: Sequence[str],
    output_cols: Sequence[str],
) -> pd.DataFrame:
    df = df.copy()
    for input_col, output_col in zip(input_cols, output_cols):
        df[output_col] = df[input_col].str.lower()
    return df

result = execute(
    input_df,
    lowercase_multi,
    schema="*,category_lower:str,value_lower:str",
    params=dict(input_cols=input_cols, output_cols=output_cols),
)

print(result.to_markdown())
"""
|    |   entity_id | category   | value   | category_lower   | value_lower   |
|---:|------------:|:-----------|:--------|:-----------------|:--------------|
|  0 |           0 | ALPHA      | FIRST   | alpha            | first         |
|  1 |           1 | BETA       | SECOND  | beta             | second        |
|  2 |           2 | GAMMA      | THIRD   | gamma            | third         |
"""
```

### 示例代码二: 多字段原位映射替换
```python
from typing import Mapping, Sequence

import pandas as pd
from mint import execute

input_df = pd.DataFrame(
    {
        "entity_id": [0, 1, 2],
        "category": ["A", "B", "C"],
        "value": ["X", "Y", "Z"],
    }
)
input_cols = ["category", "value"]
output_cols = ["category", "value"]
map_dicts = {
    "category": {"A": "alpha", "B": "beta", "C": "gamma"},
    "value": {"X": "first", "Y": "second", "Z": "third"},
}

def map_multi_columns(
    df: pd.DataFrame,
    input_cols: Sequence[str],
    output_cols: Sequence[str],
    map_dicts: Mapping[str, Mapping[str, str]],
) -> pd.DataFrame:
    df = df.copy()
    for input_col, output_col in zip(input_cols, output_cols):
        df[output_col] = df[input_col].map(map_dicts[input_col])
    return df

result = execute(
    input_df,
    map_multi_columns,
    schema="*",
    params=dict(
        input_cols=input_cols,
        output_cols=output_cols,
        map_dicts=map_dicts,
    ),
)

print(result.to_markdown())
"""
|    |   entity_id | category   | value   |
|---:|------------:|:-----------|:--------|
|  0 |           0 | alpha      | first   |
|  1 |           1 | beta       | second  |
|  2 |           2 | gamma      | third   |
"""
```

## 生成注意事项
- `input_cols` 和 `output_cols` 的顺序必须一一对应，长度也应一致。
- 新增多列时，不要使用单独的 `schema="*"`；使用 `schema="*,new_col_1:type,new_col_2:type"` 或显式声明完整 schema。
- 如果只是覆盖原字段内容，通常可以继续用 `schema="*"`。
- 多字段映射字典建议用 `dict[str, dict[str, str]]`，便于按列查找映射关系。
- 函数内部优先 `df = df.copy()`，避免原地修改输入对象。
