"""定义对象存储 adapter 的读取结果。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoredObject:
    """保存对象完整字节内容及可选 MIME 类型。"""

    data: bytes
    content_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("stored object data must be bytes")
        if self.content_type is not None and (
            not isinstance(self.content_type, str) or not self.content_type.strip()
        ):
            raise ValueError("stored object content type must not be empty")
