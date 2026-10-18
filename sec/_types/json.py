"""定义 JSON 值及 JSON 对象的递归静态类型别名。"""

from __future__ import annotations

from typing import TypeAlias

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
JSONObject: TypeAlias = dict[str, JSONValue]
