"""A parked worker must overwrite the last session's heartbeat with an idle one.

The dashboard renders the newest heartbeat row. A process that stops feeding
but never writes again leaves ``STREAMING`` and the old subscription counts on
screen for hours — observed 2026-10-12 evening, when the board showed the
morning worker (dead since 13:24, subscribed 494 under the old universe) as
if it were live until the next open. Idle beats are the fix: DISCONNECTED,
zero counts, no last tick, correct scan-mode labels.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from worker.config import WorkerSettings
from worker.main import WorkerRuntime
from worker.models import FeedStatus
from worker.persistence.memory import InMemoryStore
from worker.timeutils import load_calendar

IST = ZoneInfo("Asia/Kolkata")


def runtime(store: InMemoryStore, *, scan_mode: str = "live") -> WorkerRuntime:
    settings = WorkerSettings(
        scan_mode=scan_mode,
        simulation_label=scan_mode == "simulation",
        timezone="Asia/Kolkata",
        app_url="",
    )
    return WorkerRuntime(settings=settings, store=store, calendar=load_calendar())


async def test_idle_until_writes_an_honest_idle_beat() -> None:
    store = InMemoryStore()
    rt = runtime(store)
    past = datetime.now(IST) - timedelta(minutes=1)

    await rt._idle_until(past)  # returns immediately, but only after the beat

    beat = store.heartbeats["worker-local"]
    assert beat.status == FeedStatus.DISCONNECTED.value
    assert beat.feed_status is FeedStatus.DISCONNECTED
    assert beat.subscribed_instrument_count == 0
    assert beat.active_watchlist_count == 0
    assert beat.pending_notification_count == 0
    assert beat.last_market_data_at is None
    assert beat.last_error is None
    assert beat.scan_mode == "live"
    assert beat.simulation_label is False


async def test_idle_beat_labels_simulation_runs() -> None:
    store = InMemoryStore()
    rt = runtime(store, scan_mode="simulation")

    await rt._idle_until(datetime.now(IST) - timedelta(minutes=1))

    beat = store.heartbeats["worker-local"]
    assert beat.scan_mode == "simulation"
    assert beat.simulation_label is True


async def test_teardown_ends_the_day_disconnected() -> None:
    store = InMemoryStore()
    rt = runtime(store)

    await rt._teardown_day()

    beat = store.heartbeats["worker-local"]
    assert beat.status == FeedStatus.DISCONNECTED.value
    assert beat.feed_status is FeedStatus.DISCONNECTED


async def test_idle_beat_survives_a_store_failure() -> None:
    class FailingStore(InMemoryStore):
        async def upsert_heartbeat(self, heartbeat) -> None:  # type: ignore[override]
            raise RuntimeError("database down")

    rt = runtime(FailingStore())
    past = datetime.now(IST) - timedelta(minutes=1)

    await rt._idle_until(past)  # must not raise
