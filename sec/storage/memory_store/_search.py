"""确定性关键词分词、FTS5 查询构造与 RRF 融合。

检索的可观测性要求失败与空结果可区分；这里的纯函数不抛业务异常，
只为仓储层提供确定性的文本/排名原语。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_TERM_RE = re.compile(r"[^\W_]{2,}")
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")


def keyword_terms(text: str) -> list[str]:
    """提取至少两个字符的词元并去重，供 LIKE/评分检索使用。"""
    return list(dict.fromkeys(_TERM_RE.findall(text.lower())))


def fts5_query(text: str) -> str:
    """把文本规约为 FTS5 安全的 OR 查询；CJK 词元加前缀通配。"""
    terms = keyword_terms(text)
    if not terms:
        return ""
    return " OR ".join(
        f"{term}*" if _CJK_RE.search(term) else term for term in terms
    )


def rank_by_terms(
    rows: Sequence[tuple[str, str, float]],
    terms: Sequence[str],
) -> list[str]:
    """按关键词命中数排序；rows 元素为 (id, 检索文本, updated_at)。"""
    scored: list[tuple[int, float, str]] = []
    for item_id, haystack, updated_at in rows:
        lowered = haystack.lower()
        score = sum(1 for term in terms if term in lowered)
        if score:
            scored.append((score, updated_at, item_id))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item_id for _, _, item_id in scored]


def rrf_fuse(
    *ranked_lists: Sequence[str],
    limit: int,
    k: int = 60,
) -> list[str]:
    """以 Reciprocal Rank Fusion 合并多条按相关度降序的 id 列表。"""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    ordered = sorted(scores, key=lambda item: (-scores[item], item))
    return ordered[:limit]
