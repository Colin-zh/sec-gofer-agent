"""查询改写：把小模型改写/扩展用户消息为更利于检索的 query。

用于记忆检索或外部检索前，把口语化请求提炼成关键词查询；改写失败（响应为空）
时回退为原文。
"""

from __future__ import annotations

from dataclasses import dataclass

from sec._types.providers import ModelProvider
from sec.agent._util import complete_text

_REWRITE_SYSTEM = (
    "Rewrite the user's message into a concise keyword search query for memory "
    "or web retrieval. Expand pronouns and implicit context, drop filler words. "
    "Reply with ONLY the query text, no explanation. If the message is already "
    "a good query, return it unchanged."
)


@dataclass(frozen=True)
class RewriteResult:
    """改写后的检索 query 及是否发生变化。"""

    query: str
    changed: bool = True


class QueryRewriter:
    """用注入的 ModelProvider 改写检索 query。"""

    def __init__(self, provider: ModelProvider) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("rewrite provider must implement complete()")
        self._provider = provider

    async def rewrite(self, text: str) -> RewriteResult:
        raw = await complete_text(self._provider, _REWRITE_SYSTEM, text)
        query = raw.strip()
        if not query:
            return RewriteResult(query=text, changed=False)
        return RewriteResult(query=query, changed=query != text)
