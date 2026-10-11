"""Persistence layer.

Two interchangeable implementations of :class:`ScannerStore`:

``PostgresStore``
    the production store (Supabase / managed PostgreSQL).
``InMemoryStore``
    used by the simulator and the automated test suite, and by a local run
    before credentials exist.

The split exists so the analytics pipeline can be tested end-to-end with zero
external dependencies, while production never keeps essential state in process
memory (spec §4.3). Every semantic a test depends on — idempotency keys,
one-row-per-candle, cooldown windows — is implemented identically in both.
"""

from .memory import InMemoryStore
from .store import (
    CoverageCounts,
    NotificationClaim,
    ScannerStore,
    SignalInsertResult,
)

__all__ = [
    "CoverageCounts",
    "InMemoryStore",
    "NotificationClaim",
    "ScannerStore",
    "SignalInsertResult",
]
