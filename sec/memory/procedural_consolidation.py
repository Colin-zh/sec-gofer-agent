"""post-run 阶段从 Trace 消息中抽取用户偏好/执行规则并写入 USER.md。

与 MemoryConsolidator（语义事实）互补：语义层抽取「事实」，本组件抽取「该怎么做」的
偏好与规则，去重后写入 USER.md 的自动维护区，不覆盖手写内容。写入失败仅返回
failed 结果，不影响已完成的 Trace。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from time import time

from sec._types.content import TextContent
from sec._types.memory import ProceduralConsolidationResult
from sec._types.messages import Message, SystemMessage, UserMessage
from sec._types.providers import ModelProvider, ModelRequestOptions
from sec.exceptions import ProviderProtocolError, RetryableProviderError

_SYSTEM = (
    "Extract only long-term working preferences or rules explicitly stated by "
    "the user as something they want followed across future interactions. Do "
    "not infer a preference from one-off requests or assistant behavior, and "
    "do not extract identity facts, general facts, or event summaries. Reply "
    "with ONLY a JSON array of strings, each a concise rule in the user's "
    "voice, e.g. \"Use Chinese for responses\" or \"Prefer concise summaries\". "
    "Ignore questions, temporary task constraints, greetings, chitchat, and "
    "statements that merely describe what happened in this conversation. "
    "Return [] if the user did not explicitly state a durable preference or rule."
)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_MARK_START = "<!-- sec:procedural:start -->"
_MARK_END = "<!-- sec:procedural:end -->"
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def _normalize(rule: str) -> str:
    return " ".join(rule.split()).casefold()


class ProceduralConsolidator:
    """抽取用户偏好/执行规则并追加写入 USER.md 的自动维护区。"""

    def __init__(
        self,
        provider: ModelProvider,
        path: str | Path,
        *,
        enabled: bool = True,
    ) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError(
                "procedural consolidation provider must implement complete()"
            )
        self._provider = provider
        self._path = Path(path)
        self._enabled = enabled

    async def consolidate(
        self,
        messages: Sequence[Message],
    ) -> ProceduralConsolidationResult:
        if not self._enabled:
            return ProceduralConsolidationResult(status="empty")

        user_texts = [
            message.text.strip()
            for message in messages
            if message.role == "user" and message.text.strip()
        ]
        if not user_texts:
            return ProceduralConsolidationResult(status="empty")

        try:
            rules = await self._extract(user_texts)
        except (RetryableProviderError, ProviderProtocolError) as exc:
            return ProceduralConsolidationResult(
                status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        if not rules:
            return ProceduralConsolidationResult(status="empty")

        try:
            content = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        except (OSError, UnicodeError) as exc:
            return ProceduralConsolidationResult(
                status="failed", error=f"{type(exc).__name__}: {exc}"
            )

        merged, added = _merge_rules(content, rules)
        if not added:
            return ProceduralConsolidationResult(status="empty")

        try:
            _write_atomic(self._path, merged)
        except OSError as exc:
            return ProceduralConsolidationResult(
                status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        return ProceduralConsolidationResult(status="ok", added=tuple(added))

    async def _extract(self, texts: Sequence[str]) -> list[str]:
        messages = [
            SystemMessage(content=[TextContent(text=_SYSTEM)], timestamp=time()),
            UserMessage(
                content=[TextContent(text="\n\n".join(texts))],
                timestamp=time(),
            ),
        ]
        response = await self._provider.complete(messages, (), ModelRequestOptions())
        return _parse_rules(response.text)


def _parse_rules(text: str) -> list[str]:
    match = _JSON_ARRAY_RE.search(text)
    if match is None:
        raise ProviderProtocolError(
            "procedural consolidation response must contain a JSON array"
        )
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ProviderProtocolError(
            "procedural consolidation response contains invalid JSON"
        ) from exc
    if not isinstance(data, list):
        raise ProviderProtocolError(
            "procedural consolidation response must contain a JSON array"
        )
    rules: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            rules.append(item.strip())
    return rules


def _split_block(content: str) -> tuple[str, str, str]:
    start = content.find(_MARK_START)
    end = content.find(_MARK_END)
    if start == -1 or end == -1 or end <= start:
        return content, "", ""
    prefix = content[:start]
    block = content[start + len(_MARK_START):end]
    suffix = content[end + len(_MARK_END):]
    return prefix, block, suffix


def _parse_block_rules(block: str) -> list[str]:
    rules: list[str] = []
    for line in block.splitlines():
        match = _BULLET_RE.match(line)
        if match is not None and match.group(1).strip():
            rules.append(match.group(1).strip())
    return rules


def _merge_rules(
    content: str,
    new_rules: Sequence[str],
) -> tuple[str, tuple[str, ...]]:
    """合并新规则进自动维护区，返回 (新内容, 本次新增的规则)。

    新增规则需与既有自动规则（精确匹配）与手写内容（子串匹配）规范化去重后
    才会写入；重复运行幂等，不会覆盖手写部分。
    """
    prefix, block, suffix = _split_block(content)
    seen = {_normalize(rule) for rule in _parse_block_rules(block)}
    manual = _normalize(f"{prefix}\n{suffix}")
    added: list[str] = []
    for rule in new_rules:
        norm = _normalize(rule)
        if not norm or norm in seen or norm in manual:
            continue
        seen.add(norm)
        added.append(rule)
    if not added:
        return content, ()
    merged_rules = _parse_block_rules(block) + added
    block = "\n".join(
        [_MARK_START, *(f"- {rule}" for rule in merged_rules), _MARK_END]
    )
    parts = [part.strip() for part in (prefix, block, suffix) if part.strip()]
    return "\n\n".join(parts) + ("\n" if parts else ""), tuple(added)


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
