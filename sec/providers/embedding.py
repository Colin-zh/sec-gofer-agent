"""将自托管 OpenAI 兼容 embedding 服务接入 sec 的 Embedder 协议。

用现有 openai SDK 指向任意 OpenAI 兼容的 /embeddings 端点（如 Ollama、vLLM、
本地 embedding 服务等）；sec 不绑定具体厂商或模型。
"""

from __future__ import annotations

from collections.abc import Sequence

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from sec._types.embedding import Embedder, Embedding
from sec.exceptions import EmbedderError


class OpenAICompatibleEmbedder:
    """调用 OpenAI 兼容 embeddings 接口生成文本向量。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("embedding base_url must not be empty")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("embedding model must not be empty")
        self._model = model
        self._client = AsyncOpenAI(
            base_url=base_url,
            # 部分自托管服务不校验 key，这里用占位值兜底。
            api_key=api_key or "not-needed",
            timeout=timeout,
            max_retries=0,
        )

    async def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """返回与输入顺序一一对应的向量；失败抛出 EmbedderError。"""
        if not texts:
            return []
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=list(texts),
            )
        except (APIConnectionError, APIStatusError) as exc:
            raise EmbedderError("embedding request failed") from exc

        data = getattr(response, "data", None)
        if not data:
            raise EmbedderError("embedding response contained no data")

        vectors: list[Embedding] = []
        for item in data:
            vector = getattr(item, "embedding", None)
            if not isinstance(vector, (list, tuple)):
                raise EmbedderError("embedding response item missing a vector")
            vectors.append(tuple(float(x) for x in vector))

        if len(vectors) != len(texts):
            raise EmbedderError(
                "embedding response item count does not match the input"
            )
        return vectors

    async def aclose(self) -> None:
        """关闭底层客户端。"""
        await self._client.close()
