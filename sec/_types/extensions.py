"""Stable extension lifecycle and Run hook contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

from sec._types.events import RunTerminalStatus, SessionEvent
from sec._types.results import RunResult

if TYPE_CHECKING:
    from sec.extensions.api import ExtensionAPI


@dataclass(frozen=True)
class RunHookContext:
    """Stable identity and input data supplied to Run lifecycle hooks."""

    run_id: str
    agent_name: str
    session_id: str
    prompt: str


@dataclass(frozen=True)
class RunHookOutcome:
    """Final result or failure supplied to after-run hooks."""

    context: RunHookContext
    result: RunResult | None
    error: BaseException | None
    status: RunTerminalStatus


RunEventHook = Callable[[SessionEvent], None]
BeforeRunHook = Callable[[RunHookContext], Awaitable[None] | None]
AfterRunHook = Callable[[RunHookOutcome], Awaitable[None] | None]


class Extension(Protocol):
    """An explicitly enabled, reusable extension package entry point.

    A Client may deactivate and reactivate the same instance as its enabled set
    changes. Implementations must release owned resources on deactivation and
    support a later activation with a fresh ExtensionAPI.
    """

    name: str

    async def activate(self, api: ExtensionAPI) -> None:
        """Register contributions for this host instance."""

    async def deactivate(self) -> None:
        """Release extension-owned resources."""
