"""为模型与工具重试计算有上限的随机退避间隔。"""

from __future__ import annotations

import random


def retry_delay_seconds(
    base_delay_seconds: float,
    max_delay_seconds: float,
    *,
    failed_attempt: int,
    retry_after_seconds: float | None = None,
) -> float:
    """计算指数退避抖动，并在上限内遵循服务端建议。"""
    delay_cap = base_delay_seconds
    for _ in range(failed_attempt - 1):
        delay_cap = min(max_delay_seconds, delay_cap * 2)
        if delay_cap >= max_delay_seconds:
            break
    delay = random.uniform(0.0, delay_cap)
    if retry_after_seconds is not None:
        delay = max(delay, retry_after_seconds)
    return min(max_delay_seconds, delay)
