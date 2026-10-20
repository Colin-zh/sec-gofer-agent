"""post-run 阶段从本次 Trace 消息中抽取语义事实并写入 SemanticStore。

user 与 assistant 消息分开抽取，避免互相污染；抽取出的每条事实打 origin 标签
（user / assistant）与来源 session，写入语义仓储时按 dedup_key 幂等去重。
"""

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
    MemoryOrigin,
    MemorySource,
    memory_dedup_key,
    SemanticStoreProtocol,
)
from sec._types.messages import Message, SystemMessage, UserMessage
from sec._types.providers import ModelProvider, ModelRequestOptions
from sec.exceptions import MemoryStoreError, ProviderProtocolError, RetryableProviderError

_USER_SYSTEM = (
    "Extract durable facts and preferences stated BY the user from the given "
    "conversation messages. Reply with ONLY a JSON array of objects with "
    '"subject" and "content" keys. The subject is a short noun phrase, the '
    "content is a self-contained factual sentence. Ignore greetings, chitchat, "
    "commands, questions, and any statements about memory or system state. "
    "Return [] if there are no durable facts."
)

_ASSISTANT_SYSTEM = (
    "Extract durable facts, decisions, and results produced BY the assistant "
    "from the given conversation messages. Reply with ONLY a JSON array of "
    'objects with "subject" and "content" keys. Ignore process commentary, '
    "restated user input, and any statements about memory or system state. "
    "Return [] if there are no durable facts."
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
    """对活动分支消息做 user/assistant 分离抽取并写入语义仓储。"""

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
        assistant_texts = [
            message.text.strip()
            for message in messages
            if message.role == "assistant" and message.text.strip()
        ]
        if not user_texts and not assistant_texts:
            return MemoryConsolidationResult(status="empty")

        added_ids: list[str] = []
        errors: list[str] = []
        for origin, texts in (("user", user_texts), ("assistant", assistant_texts)):
            if not texts:
                continue
            try:
                facts = await self._extract(origin, texts)
            except (RetryableProviderError, ProviderProtocolError, MemoryStoreError) as exc:
                errors.append(f"{origin}: {type(exc).__name__}: {exc}")
                continue
            for subject, content in facts:
                try:
                    stored_id = await self._semantic.add(
                        _build_entry(origin, subject, content, session_id)
                    )
                    added_ids.append(stored_id)
                except MemoryStoreError as exc:
                    errors.append(f"{origin} write: {type(exc).__name__}: {exc}")

        if errors:
            return MemoryConsolidationResult(
                status="failed",
                added_ids=tuple(added_ids),
                error="; ".join(errors),
            )
        if not added_ids:
            return MemoryConsolidationResult(status="empty")
        return MemoryConsolidationResult(status="ok", added_ids=tuple(added_ids))

    async def _extract(
        self,
        origin: MemoryOrigin,
        texts: Sequence[str],
    ) -> list[tuple[str | None, str]]:
        system = _USER_SYSTEM if origin == "user" else _ASSISTANT_SYSTEM
        messages = [
            SystemMessage(content=[TextContent(text=system)], timestamp=time()),
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
        except json.JSONDecodeError:
            data = None
    if data is None:
        object_match = _JSON_OBJECT_RE.search(text)
        if object_match is not None:
            try:
                payload = json.loads(object_match.group(0))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                for key in ("facts", "items", "memories"):
                    candidate = payload.get(key)
                    if isinstance(candidate, list):
                        data = candidate
                        break
    if not isinstance(data, list):
        return []

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
    origin: MemoryOrigin,
    subject: str | None,
    content: str,
    session_id: str | None,
) -> MemoryEntry:
    now = time()
    return MemoryEntry(
        id=uuid4().hex,
        content=content,
        kind="semantic",
        source=MemorySource(origin=origin, session_id=session_id),
        created_at=now,
        updated_at=now,
        dedup_key=memory_dedup_key(subject, content),
        subject=subject,
        scope=DEFAULT_SCOPE,
    )
