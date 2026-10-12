"""PostgresStore.connect() lifecycle.

The original guard read `if not self._pool.open:` — but `open` is a *method*
on the pool (always truthy), not the state property, so the body never ran and
the pool stayed closed: every real database call from the worker or the sync
script then raised "the pool 'pool-1' is not open yet". The guard is
`self._pool.closed` (a property). These tests pin the fixed behavior without
touching a database: the constructor opens nothing, `connect()` must actually
call `pool.open(...)`, and a pool closed by a previous teardown must reopen.
"""

from __future__ import annotations

import asyncio

from worker.persistence.postgres import PostgresStore


class FakePool:
    """Mirrors psycopg_pool's real API shape: `closed` property, `open()` coroutine."""

    def __init__(self) -> None:
        self.closed = True
        self.open_calls = 0

    def open(self, *, wait: bool = False, timeout: float = 30.0):  # noqa: ARG002
        self.open_calls += 1

        async def _open() -> None:
            self.closed = False

        return _open()

    async def close(self) -> None:
        self.closed = True


def _store() -> PostgresStore:
    store = PostgresStore("postgresql://user:pass@example.invalid:5432/postgres")
    store._pool = FakePool()
    return store


def test_constructor_does_not_touch_the_database() -> None:
    store = _store()
    assert store._pool.closed is True  # open=False at construction


def test_connect_actually_opens_the_pool() -> None:
    store = _store()
    asyncio.run(store.connect())
    assert store._pool.open_calls == 1
    assert store._pool.closed is False


def test_connect_reopens_a_pool_closed_by_teardown() -> None:
    store = _store()
    asyncio.run(store.connect())
    store._pool.closed = True  # as after close()
    asyncio.run(store.connect())
    assert store._pool.open_calls == 2
