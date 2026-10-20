"""post-run 阶段从用户明确陈述中抽取语义事实并写入 SemanticStore。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from time import time
from uuid import uuid4

from sec._types.content import TextContent
from sec._types.memory import (
    DEFAULT_SCOPE,
    MemoryConsolidationResult,
    MemoryEntry,
    MemorySource,
    memory_dedup_key,
    SemanticStoreProtocol,
)
from sec._types.messages import Message, SystemMessage, UserMessage
from sec._types.providers import ModelProvider, ModelRequestOptions
from sec.exceptions import MemoryStoreError, ProviderProtocolError, RetryableProviderError

_USER_SYSTEM = (
    "Extract only stable factual statements explicitly stated BY the user "
    "from the given conversation "
    "messages. Reply with ONLY a JSON array of objects with "
    '"subject" and "content" keys. The subject is a short noun phrase, the '
    "content is a self-contained factual sentence. Do not infer facts from "
    "assistant messages or unstated implications. Do NOT extract preferences "
    "or instructions about how to work; those are handled separately. Ignore "
    "greetings, chitchat, commands, questions, and any statements about memory "
    "or system state. Return [] if there are no explicit stable facts."
)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_EPHEMERAL_MARKERS = (
    "no memory",
    "no relevant",
    "nothing to add",
    "no facts",
    "recall nothing",
)

class MemoryConsolidator:
    """仅从用户明确陈述抽取稳定事实并写入语义仓储。"""

    def __init__(
        self,
        provider: ModelProvider,
        semantic: SemanticStoreProtocol,
        *,
        enabled: bool = True,
    ) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("consolidation provider must implement complete()")
        self._provider = provider
        self._semantic = semantic
        self._enabled = enabled

    async def consolidate(
        self,
        messages: Sequence[Message],
        *,
        session_id: str | None = None,
    ) -> MemoryConsolidationResult:
        if not self._enabled:
            return MemoryConsolidationResult(status="empty")

        user_texts = [
            message.text.strip()
            for message in messages
            if message.role == "user" and message.text.strip()
        ]
        if not user_texts:
            return MemoryConsolidationResult(status="empty")

        added_ids: list[str] = []
        try:
            facts = await self._extract(user_texts)
        except (RetryableProviderError, ProviderProtocolError, MemoryStoreError) as exc:
            return MemoryConsolidationResult(
                status="failed",
                error=f"user: {type(exc).__name__}: {exc}",
            )
        try:
            for subject, content in facts:
                stored_id = await self._semantic.add(
                    _build_entry(subject, content, session_id)
                )
                added_ids.append(stored_id)
        except MemoryStoreError as exc:
            return MemoryConsolidationResult(
                status="failed",
                added_ids=tuple(added_ids),
                error=f"user write: {type(exc).__name__}: {exc}",
            )
        if not added_ids:
            return MemoryConsolidationResult(status="empty")
        return MemoryConsolidationResult(status="ok", added_ids=tuple(added_ids))

    async def _extract(
        self,
        texts: Sequence[str],
    ) -> list[tuple[str | None, str]]:
        messages = [
            SystemMessage(content=[TextContent(text=_USER_SYSTEM)], timestamp=time()),
            UserMessage(content=[TextContent(text="\n\n".join(texts))], timestamp=time()),
        ]
        response = await self._provider.complete(
            messages, (), ModelRequestOptions()
        )
        return _parse_facts(response.text)


def _parse_facts(text: str) -> list[tuple[str | None, str]]:
    array_match = _JSON_ARRAY_RE.search(text)
    data = None
    if array_match is not None:
        try:
            data = json.loads(array_match.group(0))
        except json.JSONDecodeError as exc:
            raise ProviderProtocolError(
                "semantic consolidation response contains invalid JSON"
            ) from exc
    if data is None:
        object_match = _JSON_OBJECT_RE.search(text)
        if object_match is not None:
            try:
                payload = json.loads(object_match.group(0))
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError(
                    "semantic consolidation response contains invalid JSON"
                ) from exc
            if isinstance(payload, dict):
                for key in ("facts", "items", "memories"):
                    candidate = payload.get(key)
                    if isinstance(candidate, list):
                        data = candidate
                        break
    if not isinstance(data, list):
        raise ProviderProtocolError(
            "semantic consolidation response must contain a JSON array"
        )

    facts: list[tuple[str | None, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject")
        content = item.get("content")
        subject = subject.strip() if isinstance(subject, str) and subject.strip() else None
        content = content.strip() if isinstance(content, str) else ""
        if not content or _is_ephemeral(content):
            continue
        facts.append((subject, content))
    return facts


def _is_ephemeral(content: str) -> bool:
    lowered = content.lower()
    return any(marker in lowered for marker in _EPHEMERAL_MARKERS)


def _build_entry(
    subject: str | None,
    content: str,
    session_id: str | None,
) -> MemoryEntry:
    now = time()
    return MemoryEntry(
        id=uuid4().hex,
        content=content,
        kind="semantic",
        source=MemorySource(origin="user", session_id=session_id),
        created_at=now,
        updated_at=now,
        dedup_key=memory_dedup_key(subject, content),
        subject=subject,
        scope=DEFAULT_SCOPE,
    )
