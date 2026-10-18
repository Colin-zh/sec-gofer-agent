---
id: execute-map-single
title: Basic mint.execute Map Single Pattern
resource_type: pattern_source
summary: 单字段映射转换模式：值替换、字典映射、大小写归一化等单列处理，可覆盖原列或新增一列。多字段成对处理请用 execute-map-multi。
tags: [execute, map, single]
related_source: [execute-map-multi, execute-schema, execute-basic]
priority: 80
---

## 适用场景
当用户基于现有单字段进行映射转换时，使用此模式。例如，用户可能需要将输入的文本转换为数字，或将日期转换为时间戳。

## 模式描述
输入一个数据框后，对其中某个字段做映射转换：
- 可以原地覆盖原字段
- 也可以生成一个新字段

如果是“原字段覆盖”，通常可以继续使用 `schema="*"`。
如果是“新增输出字段”，则应使用 `schema="*,new_col:type"` 保留现有列并声明新增列，或显式列出完整输出 schema。单独的 `schema="*"` 只包含输入中已有的列。

## 参数
### mint.execute 函数参数
- `df`: (必须) 输入数据框
- `using`: (必须) 自定义函数
- `schema`: (必须) 输出 schema 说明。是否可以写成 `*` 取决于输出列结构是否变化。

### 自定义函数参数
- `input_col`: (必须) 输入字段名。
- `output_col`: (必须) 输出字段名。
- `map_dict`: (可选) 映射字典，键为输入字段值，值为输出字段值。

## 示例
### 示例代码一: 无map_dict，直接映射输入字段到新增输出字段，定义输出schema类型
```python
import pandas as pd
from mint import execute

# 定义输入数据框
input_df = pd.DataFrame({"entity_id": [0, 1, 2], "value": ["a", "b", "c"]})
input_col = "value"
output_col = "value_lowercase"

# 打印输入数据框
print(input_df.to_markdown())
"""
|    |   entity_id | value   |
|---:|------------:|:--------|
|  0 |           0 | a       |
|  1 |           1 | b       |
|  2 |           2 | c       |
"""

# 需求将value字段转换为小写字符
def convert_lowercase(
    df: pd.DataFrame, input_col: str, output_col: str
) -> pd.DataFrame:
    df = df.copy()
    df[output_col] = df[input_col].str.lower()
    return df

# 保留输入列，并声明新增列的类型
output_schema = "*,value_lowercase:str"

# 执行
result = execute(
    input_df,
    convert_lowercase,
    schema=output_schema,
    params=dict(input_col=input_col, output_col=output_col),
)

# 打印输出数据框
print(result.to_markdown())
"""
|    |   entity_id | value   | value_lowercase   |
|---:|------------:|:--------|:------------------|
|  0 |           0 | a       | a                 |
|  1 |           1 | b       | b                 |
|  2 |           2 | c       | c                 |
"""
```

### 示例代码二: 原字段映射替换
```python
from typing import Dict

import pandas as pd
from mint import execute

# 定义输入数据框
input_df = pd.DataFrame({"entity_id": [0, 1, 2], "value": ["a", "b", "c"]})
map_dict = {"a": "alpha", "b": "beta", "c": "gamma"}
input_col = "value"
output_col = "value"

# 打印输入数据框
print(input_df.to_markdown())
"""
|    |   entity_id | value   |
|---:|------------:|:--------|
|  0 |           0 | a       |
|  1 |           1 | b       |
|  2 |           2 | c       |
"""

# 需求将value字段基于map_dict进行映射转换
def map_value(
    df: pd.DataFrame, 
    input_col: str,
    output_col: str,
    map_dict: Dict[str, str]
) -> pd.DataFrame:
    df = df.copy()
    df[output_col] = df[input_col].map(map_dict)
    return df

# 定义输出类型
output_schema = "*"

# 执行
result = execute(
    input_df,
    map_value,
    schema=output_schema,
    params=dict(
        input_col=input_col, 
        output_col=output_col, 
        map_dict=map_dict
    ),
)

# 打印输出数据框
print(result.to_markdown())
"""
|    |   entity_id | value   |
|---:|------------:|:--------|
|  0 |           0 | alpha   |
|  1 |           1 | beta    |
|  2 |           2 | gamma   |
"""
```

## 生成注意事项
- 判断是否可以使用 `schema="*"` 的关键标准，是输出列结构是否与输入保持一致。
- 新增列时使用 `schema="*,new_col:type"`（例如 `*,value_lowercase:str`）或显式列出完整输出 schema；单独的 `schema="*"` 不会包含新增列。
- 如果删除列或重命名列，必须显式列出完整输出 schema，例如 `entity_id:long,value:str,value_lowercase:str`。
- 如果只是覆盖原字段内容，通常可以继续用 `schema="*"`。
- 优先通过 `params=dict(...)` 传入字段名、映射字典等处理参数。
- 示例函数内部优先 `df = df.copy()`，避免原地修改输入对象带来副作用。
