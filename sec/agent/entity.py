"""实体抽取：用小模型从文本中抽取命名实体。

可作为独立可复用组件（当前未接入主循环）；与记忆事实抽取（MemoryConsolidator）
互补——后者抽取持久事实/偏好，本组件只抽取实体名，供上下文富化或检索排序使用。
"""

from __future__ import annotations

from dataclasses import dataclass

from sec._types.providers import ModelProvider
from sec.agent._util import complete_text, parse_json_array

_ENTITY_SYSTEM = (
    "Extract named entities from the text: people, organizations, locations, "
    "dates, technical terms, file paths, and identifiers. Reply with ONLY a "
    "JSON array of strings, deduplicated. Return [] if there are none."
)


@dataclass(frozen=True)
class EntityExtraction:
    """抽取出的实体名列表。"""

    entities: tuple[str, ...] = ()


class EntityExtractor:
    """用注入的 ModelProvider 抽取命名实体。"""

    def __init__(self, provider: ModelProvider) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("entity provider must implement complete()")
        self._provider = provider

    async def extract(self, text: str) -> EntityExtraction:
        raw = await complete_text(self._provider, _ENTITY_SYSTEM, text)
        data = parse_json_array(raw)
        if data is None:
            return EntityExtraction()
        entities = [item.strip() for item in data if isinstance(item, str) and item.strip()]
        return EntityExtraction(entities=tuple(dict.fromkeys(entities)))
