"""The purge runs while the market sleeps, and only while it sleeps.

Two failure modes this pins, both observed on the live pooler while §0009 was
being rolled out. First, a worker that boots after the close used to reach
``_wait_for_next_session`` without ever ageing its tables: ``phase_at`` at 22:17
IST is CLOSED, and ``_run_today``'s CLOSED+FINALIZED branch returns 0 — only
``run()``\'s single ``_idle_maintenance`` hook is on every one of those paths.
Second, the sweep must not fall inside the trading window: that DELETE is the
second most expensive statement in the database (3.1 s), and at 09:15 it
competes with the warm-up read and the first scans of the day.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from worker import main as worker_main
from worker.config import WorkerSettings
from worker.main import WorkerRuntime
from worker.persistence.memory import InMemoryStore
from worker.timeutils import SessionPhase, load_calendar

IST = ZoneInfo("Asia/Kolkata")


def runtime(store: InMemoryStore) -> WorkerRuntime:
    settings = WorkerSettings(
        scan_mode="live",
        simulation_label=False,
        timezone="Asia/Kolkata",
        app_url="",
        retention_days=3,
    )
    return WorkerRuntime(settings=settings, store=store, calendar=load_calendar())


def seed(store: InMemoryStore, *, age_days: float, instrument_id: int = 1) -> None:
    """One candle ``age_days`` old, and a snapshot from the same minute.

    ``purge_older_than`` only looks at the key, so a placeholder row is enough
    to show whether the DELETE reached it — and the placeholder stays
    honest-looking because the store holds the pair.
    """
    start = datetime.now(IST) - timedelta(days=age_days)
    store.candles[(instrument_id, start)] = None  # type: ignore[assignment]
    store.snapshots[(instrument_id, start)] = None  # type: ignore[assignment]


async def test_idle_maintenance_ages_the_tables_after_the_close() -> None:
    store = InMemoryStore()
    seed(store, age_days=9)

    await runtime(store)._idle_maintenance()

    assert store.candles == {}
    assert store.snapshots == {}


async def test_idle_maintenance_respects_the_saved_window() -> None:
    store = InMemoryStore()
    store.ops_settings["retention"] = {"days": 3}
    seed(store, age_days=4, instrument_id=1)
    seed(store, age_days=2, instrument_id=2)

    await runtime(store)._idle_maintenance()

    assert [key[0] for key in store.candles] == [2]  # 3 days kept, day 4 gone
    assert [key[0] for key in store.snapshots] == [2]


async def test_a_zero_window_means_keep_forever_not_delete_everything() -> None:
    """The staging install (Mini) accumulates history for backtests: the
    dashboard writes 0, and the purge must skip — not clamp 0 to a one-day
    window whose next sweep would clear almost the entire table."""
    store = InMemoryStore()
    store.ops_settings["retention"] = {"days": 0}
    seed(store, age_days=400)

    rt = runtime(store)
    assert await rt._retention_days() is None

    await rt._idle_maintenance()

    assert len(store.candles) == 1  # 400 days old and still there
    assert len(store.snapshots) == 1


async def test_a_negative_window_is_also_forever_but_junk_falls_back() -> None:
    """Only a real number <= 0 disables ageing. An unreadable value must keep
    the old behaviour — fall back to the env default — so corruption cannot
    quietly turn Mumbai into a never-purging database."""
    store = InMemoryStore()
    rt = runtime(store)

    store.ops_settings["retention"] = {"days": -1}
    assert await rt._retention_days() is None

    store.ops_settings["retention"] = {"days": "junk"}
    assert await rt._retention_days() == 3  # the env default, not "forever"

    store.ops_settings.pop("retention")
    assert await rt._retention_days() == 3


class FixedPhaseCalendar:
    """A calendar that answers one question, so the test pins the schedule
    rather than this year's holiday file. ``_market_is_live`` reads the phase
    and nothing else."""

    def __init__(self, phase: SessionPhase) -> None:
        self._phase = phase

    def phase_at(self, moment: datetime) -> SessionPhase:
        return self._phase


async def test_idle_maintenance_is_quiet_while_the_session_is_live() -> None:
    store = InMemoryStore()
    seed(store, age_days=9)
    rt = runtime(store)
    rt.window = object()  # a session is open; the phase is what decides
    rt.calendar = FixedPhaseCalendar(SessionPhase.ACTIVE)  # type: ignore[assignment]

    await rt._idle_maintenance()

    assert len(store.candles) == 1  # nothing deleted, the board is being read


async def test_an_evening_boot_is_not_live(monkeypatch) -> None:
    """The trap that made the 22:17 restart skip the purge: ``phase_at`` after
    ``post_close_end_at`` is CLOSED, not POST_CLOSE, so the sweep must key off
    "not pre-open and not active" rather than off a specific phase."""
    rt = runtime(InMemoryStore())
    rt.window = object()
    evening = datetime(2026, 10, 14, 22, 17, tzinfo=IST)
    monkeypatch.setattr(worker_main, "now_ist", lambda: evening)
    assert rt._market_is_live(evening) is False

    await rt._idle_maintenance()  # reaches the purge, does not raise



async def test_the_sweep_survives_a_store_failure() -> None:
    class FailingStore(InMemoryStore):
        async def purge_older_than(self, days: int, *, now: datetime) -> dict[str, int]:
            raise RuntimeError("database down")

        async def get_ops_setting(self, key: str) -> dict[str, object] | None:
            raise RuntimeError("database down")

    await runtime(FailingStore())._idle_maintenance()  # must not raise
