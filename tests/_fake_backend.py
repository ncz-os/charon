"""Focused fake persistence backend for document-import route tests.

The document import surface is backend-neutral.  These tests therefore use a
recording implementation of the repository methods exercised by the route,
rather than emulating a driver connection or issuing real database writes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any


class _FakeMemoryRepository:
    """Record document memory inserts and return their canonical IDs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def insert_memory(self, tx: Any, **kwargs: Any) -> str:
        self.calls.append(("insert_memory", kwargs))
        return str(kwargs["memory_id"])


class _FakeWebhookRepository:
    """Record transactional outbox events emitted by document imports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch_event(
        self,
        tx: Any,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str | None = None,
        namespace: str | None = None,
    ) -> list[str]:
        self.calls.append(
            (
                "dispatch_event",
                {
                    "event_type": event_type,
                    "payload": payload,
                    "owner_id": owner_id,
                    "namespace": namespace,
                },
            )
        )
        return []


class FakeBackend:
    """Persistence-backend implementation required by document import tests."""

    supports_webhooks = True

    def __init__(self) -> None:
        self.memories = _FakeMemoryRepository()
        self.webhooks = _FakeWebhookRepository()
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transactional(self):
        tx = SimpleNamespace(_fake=True, conn=SimpleNamespace())
        try:
            yield tx
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    async def close(self) -> None:
        return None


def install_fake_backend(monkeypatch: Any) -> FakeBackend:
    """Install an isolated recording backend in the lifecycle singleton."""
    import mnemos.core.lifecycle as lifecycle

    backend = FakeBackend()
    monkeypatch.setattr(lifecycle, "_pool", None)
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(lifecycle, "_rls_enabled", False)
    monkeypatch.setattr(lifecycle, "_cache", None)
    return backend
