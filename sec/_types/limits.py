"""Timeout and resource limits for requests, runs, and event collection."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields


def validate_timeout(value: object, name: str = "timeout") -> None:
    """Require a positive finite numeric timeout."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            if math.isfinite(value):
                return
        except OverflowError:
            pass
    raise ValueError(f"{name} must be a positive finite number")


@dataclass(frozen=True)
class Limits:
    """Set timeouts and storage bounds for transport and event processing."""

    startup_timeout: float = 30.0
    command_timeout: float = 30.0
    run_start_timeout: float = 30.0
    cleanup_timeout: float = 5.0
    max_record_bytes: int = 16 * 1024 * 1024
    event_queue_size: int = 256
    event_queue_bytes: int = 16 * 1024 * 1024
    stderr_tail_bytes: int = 0
    result_message_count: int = 4096
    result_message_bytes: int = 64 * 1024 * 1024
    collection_event_count: int = 16_384
    collection_event_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for descriptor in fields(self):
            name = descriptor.name
            value = getattr(self, name)
            if name.endswith("timeout"):
                validate_timeout(value, name)
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (0 if name == "stderr_tail_bytes" else 1)
            ):
                minimum = "nonnegative" if name == "stderr_tail_bytes" else "positive"
                raise ValueError(f"{name} must be a {minimum} integer")


__all__ = ["Limits", "validate_timeout"]
