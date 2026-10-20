"""定义长期记忆向量化所需的 embedding 行为契约。

Embedder 是可替换组件；sec 不绑定具体厂商或模型。自托管 OpenAI 兼容实现
位于 sec/providers/embedding.py。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeAlias, runtime_checkable

Embedding: TypeAlias = tuple[float, ...]


@runtime_checkable
class Embedder(Protocol):
    """将文本序列映射为等长浮点向量。"""

    async def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """返回与输入顺序一一对应的向量；失败抛出 EmbedderError。"""

    async def aclose(self) -> None:
        """释放底层客户端资源；无资源时可不执行操作。"""


__all__ = ["Embedding", "Embedder"]
