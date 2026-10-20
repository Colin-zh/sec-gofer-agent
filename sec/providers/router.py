"""双 tier 模型路由：main 负责核心推理，small 处理低复杂度辅助场景。

Router 拥有 main 与 small 两个 Provider；small 未配置时 provider("small")
回退到 main，保证所有辅助场景在无小模型时透明降级。调用方通过 aclose()
关闭两者，同一实例只关闭一次。
"""

from __future__ import annotations

from typing import Literal

from sec._types.providers import ModelProvider, StreamingModelProvider

ModelTier = Literal["main", "small"]


class ModelRouter:
    """在 main / small 两个模型 tier 之间选择 Provider。"""

    def __init__(
        self,
        main: ModelProvider | StreamingModelProvider,
        small: ModelProvider | StreamingModelProvider | None = None,
    ) -> None:
        self._main = main
        self._small = small

    @property
    def main(self) -> ModelProvider | StreamingModelProvider:
        """返回主模型 Provider。"""
        return self._main

    @property
    def small(self) -> ModelProvider | StreamingModelProvider | None:
        """返回小模型 Provider；未配置时为 None。"""
        return self._small

    def provider(
        self,
        tier: ModelTier = "main",
    ) -> ModelProvider | StreamingModelProvider:
        """按 tier 返回 Provider；small 缺失时回退 main。"""
        if tier == "small" and self._small is not None:
            return self._small
        return self._main

    async def aclose(self) -> None:
        """关闭 main 与 small；同一实例只关闭一次。"""
        closed: set[int] = set()
        for provider in (self._main, self._small):
            if provider is None:
                continue
            identity = id(provider)
            if identity in closed:
                continue
            closed.add(identity)
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
