"""Small, testable helpers for owning one scheduled event at a time."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar


class CancellableEvent(Protocol):
    def cancel(self) -> object:
        """Cancel future callbacks."""


Event = TypeVar("Event", bound=CancellableEvent)


def cancel_event(event: CancellableEvent | None) -> None:
    """Cancel an event when present, tolerating an already-cancelled handle."""

    if event is None:
        return
    try:
        event.cancel()
    except Exception:
        return


def replace_event(event: Event | None, factory: Callable[[], Event]) -> Event:
    """Cancel an old handle before creating and returning its replacement."""

    cancel_event(event)
    return factory()

