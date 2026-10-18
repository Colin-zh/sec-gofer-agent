"""规范化 Provider 临时 HTTP 错误的重试信息。

该模块只解析 HTTP 状态与 Retry-After，不依赖具体 SDK；
供应商适配器决定哪些 SDK 异常属于连接错误或状态错误。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sec.exceptions import RetryableProviderError


def retryable_status_error(
    provider_name: str,
    status_code: int,
    headers: Mapping[str, str],
) -> RetryableProviderError | None:
    """为暂时性 HTTP 状态构造可重试异常。"""
    if status_code not in (408, 409, 429) and status_code < 500:
        return None
    if status_code >= 600:
        return None
    return RetryableProviderError(
        f"{provider_name} request failed with retryable HTTP status {status_code}",
        retry_after_seconds=_retry_after_seconds(headers),
        status_code=status_code,
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)
