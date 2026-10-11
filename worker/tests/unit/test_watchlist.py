"""Behavioural tests for the daily watchlist state machine (spec §11).

Each test feeds candles through :meth:`WatchlistManager.on_candle` with explicit
``bullish=``/``evaluable=`` flags so that one spec rule is isolated per test.
Section comments (# §n) map to the numbered product rules in the brief.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import BASE_DAY, candle, minute
from worker.alerts import AlertEngine
from worker.models import (
    DataQuality,
    IndicatorSnapshot,
    Instrument,
    WatchlistMembership,
    WatchlistState,
)
from worker.persistence.memory import InMemoryStore
from worker.watchlist import WatchlistManager

REASONS = ("CLOSE_ABOVE_VWAP", "EMA9_ABOVE_EMA20", "RELATIVE_VOLUME_ABOVE_THRESHOLD")


def inst(id: int = 1, symbol: str = "RELIANCE") -> Instrument:
    return Instrument(
        id=id,
        exchange="NSE",
        symbol=symbol,
        trading_symbol=symbol,
        company_name=f"{symbol} Industries Ltd",
        instrument_token=str(1000 + id),
    )


def snap(
    close: float = 105.0,
    vwap: float | None = 100.0,
    ema_fast: float | None = 103.0,
    ema_slow: float | None = 101.0,
    relative_volume: float | None = 1.8,
    **overrides: Any,
) -> IndicatorSnapshot:
    """A valid, evaluable snapshot; override only what a test cares about."""
    base: dict[str, Any] = {
        "instrument_id": 1,
        "trading_date": BASE_DAY,
        "candle_start": minute(0),
        "vwap": vwap,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "average_volume": 1000.0,
        "relative_volume": relative_volume,
        "intraday_change_pct": 5.0,
        "distance_from_vwap_pct": 5.0,
        "ema_spread_pct": 2.0,
        "close": close,
        "previous_close": 100.0,
        "session_high": None,
        "session_low": None,
        "distance_from_high_pct": None,
        "bullish_qualified": True,
        "reasons": REASONS,
        "failed_reasons": (),
        "data_quality": DataQuality.OK,
        "warming_up": False,
    }
    base.update(overrides)
    return IndicatorSnapshot(**base)


def weakening_snap(**kw: Any) -> IndicatorSnapshot:
    # close < VWAP, EMA still aligned -> §2 weakening via one leg, never a
    # removal candle.
    kw.setdefault("close", 99.0)
    return snap(**kw)


def removal_snap(**kw: Any) -> IndicatorSnapshot:
    # close < VWAP *and* EMA9 <= EMA20 on the same candle (§3 predicate).
    kw.setdefault("close", 99.0)
    kw.setdefault("ema_fast", 100.0)
    kw.setdefault("ema_slow", 101.0)
    return snap(**kw)


async def feed(
    manager: WatchlistManager,
    instrument: Instrument,
    snapshot: IndicatorSnapshot,
    *,
    offset: int,
    bullish: bool,
    evaluable: bool = True,
    session_id: int = 1,
    **candle_kwargs: Any,
):
    bar = candle(
        close=snapshot.close,
        offset_minutes=offset,
        instrument_id=instrument.id,
        **candle_kwargs,
    )
    return await manager.on_candle(
        session_id=session_id,
        trading_date=BASE_DAY,
        instrument=instrument,
        candle=bar,
        snapshot=snapshot,
        bullish=bullish,
        evaluable=evaluable,
        reasons=REASONS if bullish else (),
        now=minute(offset),
    )


def row(store: InMemoryStore, instrument_id: int = 1, session_id: int = 1) -> WatchlistMembership:
    return store.memberships[store.membership_keys[(session_id, instrument_id)]]


def event_types(store: InMemoryStore) -> list[str]:
    return [event.event_type for event in store.events]


@pytest.fixture()
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture()
def manager(store: InMemoryStore, params) -> WatchlistManager:
    return WatchlistManager(store=store, params=params)


@pytest.fixture()
def alert_manager(store: InMemoryStore, params) -> WatchlistManager:
    engine = AlertEngine(store=store, params=params)
    return WatchlistManager(store=store, params=params, alerts=engine)


# ── §1 qualification ────────────────────────────────────────────────────────


async def test_qualifying_candle_creates_active_membership(manager):
    # §1 close>VWAP + EMA9>EMA20 + RV>=1.5 — driven here as bullish=True on a
    # valid completed candle — is what ADDS a row.
    outcome = await feed(manager, inst(), snap(candle_start=minute(5)), offset=5, bullish=True)

    assert outcome.state is WatchlistState.ACTIVE
    assert outcome.changed is True
    assert outcome.event_type == "ADDED"
    assert outcome.episode_id == f"{BASE_DAY.isoformat()}-1-e1"
    stored = row(manager.store)
    assert stored.current_state is WatchlistState.ACTIVE
    assert stored.is_active is True
    assert stored.episode_count == 1
    assert stored.initial_entry_price == 105.0


async def test_bullish_without_evaluable_never_adds(manager):
    # §1 qualification also requires the candle to be *evaluable*; a
    # warm-up-only bullish print must not create a membership row.
    outcome = await feed(
        manager, inst(), snap(candle_start=minute(5)), offset=5, bullish=True, evaluable=False
    )
    assert outcome.state is WatchlistState.WARMING_UP
    assert outcome.changed is False
    assert manager.store.memberships == {}


async def test_non_bullish_candle_never_creates_row(manager):
    # §1 non-qualification writes nothing: no row, no event history.
    outcome = await feed(manager, inst(), removal_snap(candle_start=minute(5)), offset=5, bullish=False)
    assert outcome.changed is False
    assert manager.store.memberships == {}
    assert manager.store.events == []


# ── §2 WEAKENING ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "weak",
    [{"close": 99.0}, {"ema_fast": 100.0, "ema_slow": 101.0}],
    ids=["close_below_vwap", "ema9_at_or_below_ema20"],
)
async def test_either_condition_marks_weakening_and_row_stays_active(manager, weak):
    # §2.1 WEAKENING when close < VWAP *or* EMA9 <= EMA20 — either leg alone.
    # §2.2 the row stays visible/active (is_active True).
    await feed(manager, inst(), snap(candle_start=minute(5)), offset=5, bullish=True)
    outcome = await feed(
        manager, inst(), snap(candle_start=minute(6), **weak), offset=6, bullish=False
    )

    assert outcome.state is WatchlistState.WEAKENING
    assert outcome.changed is True
    assert outcome.event_type == "WEAKENING"
    stored = row(manager.store)
    assert stored.current_state is WatchlistState.WEAKENING
    assert stored.is_active is True  # §2 "the row stays visible/active"
    last = manager.store.events[-1]
    assert last.previous_state is WatchlistState.ACTIVE
    assert last.new_state is WatchlistState.WEAKENING


async def test_bullish_candle_recovers_weakening_to_active(manager):
    # §2.3 a tracked row that regains all conditions returns to ACTIVE and the
    # recovery is appended to the event history (§4 append-only). The admission
    # itself is logged too: the event stream starts with ADDED, not with the
    # first deterioration (§11.7).
    await feed(manager, inst(), snap(candle_start=minute(5)), offset=5, bullish=True)
    await feed(manager, inst(), weakening_snap(candle_start=minute(6)), offset=6, bullish=False)
    outcome = await feed(manager, inst(), snap(candle_start=minute(7)), offset=7, bullish=True)

    assert outcome.state is WatchlistState.ACTIVE
    assert outcome.event_type == "RECOVERY_TO_ACTIVE"
    assert event_types(manager.store) == ["ADDED", "WEAKENING", "RECOVERY_TO_ACTIVE"]
    assert row(manager.store).is_active is True


# ── §3 removal ───────────────────────────────────────────────────────────────


async def test_removal_needs_three_consecutive_valid_candles(manager):
    # §3.1 the first two both-bad candles only weaken (streak 1, 2); the third
    # consecutive removal-condition candle REMOVES the row.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    for offset in (6, 7):
        outcome = await feed(
            manager, instrument, removal_snap(candle_start=minute(offset)), offset=offset, bullish=False
        )
        assert outcome.state is WatchlistState.WEAKENING
        assert row(manager.store).is_active is True
    assert row(manager.store).consecutive_weak_candles == 2

    third = await feed(manager, instrument, removal_snap(candle_start=minute(8)), offset=8, bullish=False)
    assert third.state is WatchlistState.REMOVED
    assert third.event_type == "REMOVED"
    assert third.changed is True
    stored = row(manager.store)
    assert stored.current_state is WatchlistState.REMOVED
    assert stored.is_active is False
    assert stored.removal_reason == "PERSISTENT_BELOW_VWAP_AND_EMA_CROSS_DOWN"
    assert stored.consecutive_weak_candles == 3


async def test_one_non_removal_candle_breaks_the_streak(manager):
    # §3.2 the three candles must be *consecutive*: a valid candle that fails
    # only one leg resets the streak, so two more removal candles are not
    # enough to remove.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    await feed(manager, instrument, removal_snap(candle_start=minute(6)), offset=6, bullish=False)
    await feed(manager, instrument, removal_snap(candle_start=minute(7)), offset=7, bullish=False)
    # A single-leg weakening candle is valid evidence the run was broken.
    await feed(manager, instrument, weakening_snap(candle_start=minute(8)), offset=8, bullish=False)
    assert row(manager.store).consecutive_weak_candles == 0

    await feed(manager, instrument, removal_snap(candle_start=minute(9)), offset=9, bullish=False)
    await feed(manager, instrument, removal_snap(candle_start=minute(10)), offset=10, bullish=False)
    assert row(manager.store).current_state is WatchlistState.WEAKENING
    assert row(manager.store).consecutive_weak_candles == 2

    final = await feed(manager, instrument, removal_snap(candle_start=minute(11)), offset=11, bullish=False)
    assert final.state is WatchlistState.REMOVED


async def test_incomplete_candle_neither_advances_nor_resets_streak(manager):
    # §3.3/§5 an incomplete candle in the middle parks the row (a feed gap is
    # not evidence): the pending streak survives and the next removal candle
    # is still #3, so the row is REMOVED.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    await feed(manager, instrument, removal_snap(candle_start=minute(6)), offset=6, bullish=False)
    await feed(manager, instrument, removal_snap(candle_start=minute(7)), offset=7, bullish=False)
    gap = await feed(
        manager,
        instrument,
        removal_snap(candle_start=minute(8)),
        offset=8,
        bullish=False,
        is_complete=False,
    )
    assert gap.state is WatchlistState.DATA_STALE
    assert row(manager.store).consecutive_weak_candles == 2  # not advanced, not reset

    third = await feed(manager, instrument, removal_snap(candle_start=minute(9)), offset=9, bullish=False)
    assert third.state is WatchlistState.REMOVED
    assert row(manager.store).is_active is False


# ── §5 unusable candles ──────────────────────────────────────────────────────


async def test_unusable_candle_never_creates_membership(manager):
    # §5.1 a bullish-looking but incomplete candle must not create a row.
    outcome = await feed(
        manager,
        inst(),
        snap(candle_start=minute(5)),
        offset=5,
        bullish=True,
        is_complete=False,
    )
    assert outcome.state is WatchlistState.WARMING_UP
    assert outcome.changed is False
    assert manager.store.memberships == {}
    assert manager.store.events == []
    assert manager.skipped_unusable == {1}


async def test_bad_quality_candle_parks_tracked_row_without_other_changes(manager):
    # §5.2 for an already-tracked row an unusable (STALE-quality) candle parks
    # it as DATA_STALE — still active, same episode, nothing else changes.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    manager.store.events.clear()

    outcome = await feed(
        manager,
        instrument,
        snap(candle_start=minute(6)),
        offset=6,
        bullish=True,
        data_quality=DataQuality.STALE,
    )
    assert outcome.state is WatchlistState.DATA_STALE
    stored = row(manager.store)
    assert stored.current_state is WatchlistState.DATA_STALE
    assert stored.is_active is True  # parked, not dropped
    assert stored.episode_count == 1
    assert event_types(manager.store) == ["DATA_STALE"]
    assert manager.skipped_unusable == {instrument.id}


# ── §4 rows, event history and re-entry ─────────────────────────────────────


async def test_one_membership_row_per_session_and_instrument(manager):
    # §4.1 add -> remove -> re-enter keeps exactly one daily_watchlists row.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    for offset in (6, 7, 8):
        await feed(
            manager, instrument, removal_snap(candle_start=minute(offset)), offset=offset, bullish=False
        )
    assert row(manager.store).current_state is WatchlistState.REMOVED
    membership_id = row(manager.store).id

    reentry = await feed(manager, instrument, snap(candle_start=minute(9)), offset=9, bullish=True)
    assert reentry.state is WatchlistState.REQUALIFIED
    assert len(manager.store.memberships) == 1
    assert row(manager.store).id == membership_id
    types = event_types(manager.store)
    assert "REMOVED" in types
    assert types[-1] == "REQUALIFIED"
    assert manager.store.events[-1].detail["episode_count"] == 2


async def test_reentry_after_removal_mints_new_episode(manager):
    # §4.2 re-qualification increments episode_count and returns a NEW episode
    # id (fresh -e2 suffix) while clearing removal bookkeeping.
    instrument = inst()
    first = await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    assert first.episode_id == f"{BASE_DAY.isoformat()}-1-e1"
    for offset in (6, 7, 8):
        await feed(
            manager, instrument, removal_snap(candle_start=minute(offset)), offset=offset, bullish=False
        )

    second = await feed(manager, instrument, snap(candle_start=minute(9)), offset=9, bullish=True)
    assert second.episode_id == f"{BASE_DAY.isoformat()}-1-e2"
    assert second.changed is True
    stored = row(manager.store)
    assert stored.episode_count == 2
    assert stored.current_state is WatchlistState.REQUALIFIED
    assert stored.is_active is True
    assert stored.consecutive_weak_candles == 0
    assert stored.removal_reason in (None, "")


async def test_streak_weakening_transition_is_recorded_in_event_history(manager):
    # §4 ACTIVE->WEAKENING driven by the removal streak must also land in the
    # append-only event log, not just predicate-driven weakening.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    outcome = await feed(
        manager, instrument, removal_snap(candle_start=minute(6)), offset=6, bullish=False
    )
    assert outcome.state is WatchlistState.WEAKENING
    assert outcome.changed is True
    weakening_events = [e for e in manager.store.events if e.new_state is WatchlistState.WEAKENING]
    assert len(weakening_events) == 1


async def test_recovery_to_active_clears_the_removal_streak(manager):
    # §3 recovery breaks the "consecutive" requirement: after
    # ACTIVE->WEAKENING(streak 2)->ACTIVE, one removal candle must NOT reach
    # the 3-candle threshold.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    await feed(manager, instrument, removal_snap(candle_start=minute(6)), offset=6, bullish=False)
    await feed(manager, instrument, removal_snap(candle_start=minute(7)), offset=7, bullish=False)
    recovered = await feed(manager, instrument, snap(candle_start=minute(8)), offset=8, bullish=True)
    assert recovered.state is WatchlistState.ACTIVE

    after = await feed(
        manager, instrument, removal_snap(candle_start=minute(9)), offset=9, bullish=False
    )
    assert after.state is not WatchlistState.REMOVED  # streak should restart at 1
    assert row(manager.store).is_active is True


async def test_removed_row_does_not_resurrect_without_qualification(manager):
    # §4 once REMOVED, only a bullish re-qualification may bring the row back;
    # a weakening candle must leave it removed and inactive.
    instrument = inst()
    await feed(manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True)
    for offset in (6, 7, 8):
        await feed(
            manager, instrument, removal_snap(candle_start=minute(offset)), offset=offset, bullish=False
        )
    assert row(manager.store).current_state is WatchlistState.REMOVED

    await feed(manager, instrument, weakening_snap(candle_start=minute(9)), offset=9, bullish=False)
    stored = row(manager.store)
    assert stored.current_state is WatchlistState.REMOVED
    assert stored.is_active is False


# ── §6 idempotency through the manager ──────────────────────────────────────


async def test_reprocessing_bullish_candle_does_not_mint_signals(alert_manager):
    # §6 once ACTIVE, another bullish candle on the same episode must not
    # re-raise the entry alert pair.
    instrument = inst()
    first = await feed(
        alert_manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True
    )
    assert first.signaled is True
    assert len(alert_manager.store.signals) == 2  # WATCHLIST_ADDED + BULLISH_ENTRY

    again = await feed(
        alert_manager, instrument, snap(candle_start=minute(6)), offset=6, bullish=True
    )
    assert again.signaled is False
    assert again.reason == "still qualified"
    assert len(alert_manager.store.signals) == 2


# ── §11 initial scan ─────────────────────────────────────────────────────────


async def test_initial_scan_ranks_qualifiers_and_writes_latest_rank(manager):
    # §11.1 counts by category, score-descending `ranked`, latest_rank written
    # onto each qualifying membership, missing_data for no-evaluation names.
    store = manager.store
    strong = inst(id=1, symbol="RELIANCE")
    weak = inst(id=2, symbol="TATAMOTORS")
    dud = inst(id=3, symbol="INFY")
    missing = inst(id=4, symbol="HDFCBANK")

    strong_snap = snap(
        instrument_id=1,
        candle_start=minute(40),
        close=108.0,
        vwap=104.0,
        ema_fast=106.0,
        ema_slow=104.5,
        relative_volume=4.0,
        intraday_change_pct=8.0,
        distance_from_vwap_pct=4.0,
        ema_spread_pct=3.0,
    )
    weak_snap = snap(
        instrument_id=2,
        candle_start=minute(40),
        close=101.0,
        vwap=100.0,
        ema_fast=101.0,
        ema_slow=100.5,
        relative_volume=1.6,
        intraday_change_pct=1.0,
        distance_from_vwap_pct=0.5,
        ema_spread_pct=0.2,
    )
    latest = {
        3: (
            candle(close=99.0, offset_minutes=40, instrument_id=3),
            weakening_snap(instrument_id=3, candle_start=minute(40)),
            False,
            True,
            (),
        ),
        2: (
            candle(close=101.0, offset_minutes=40, instrument_id=2),
            weak_snap,
            True,
            True,
            REASONS,
        ),
        1: (
            candle(close=108.0, offset_minutes=40, instrument_id=1),
            strong_snap,
            True,
            True,
            REASONS,
        ),
    }
    summary = await manager.initial_scan(
        session_id=1,
        trading_date=BASE_DAY,
        instruments=[strong, weak, dud, missing],
        latest=latest,
        now=minute(40),
    )

    assert summary["kind"] == "initial_scan"
    assert summary["universe"] == 4
    counts = summary["counts"]
    assert counts["evaluated"] == 3
    assert counts["qualified"] == 2
    assert counts["not_qualified"] == 1
    assert counts["missing_data"] == 1
    assert summary["ranked"] == ["RELIANCE", "TATAMOTORS"]  # score-descending
    assert row(store, 1).latest_rank == 1  # §11 rank write
    assert row(store, 2).latest_rank == 2
    assert summary["candles_required"] == manager.params.warmup_candles_required


async def test_initial_scan_not_evaluable_counts_as_warming_up(manager):
    # §11.2 an instrument whose indicators are not ready is counted as
    # warming_up and never gets a row.
    instrument = inst()
    summary = await manager.initial_scan(
        session_id=1,
        trading_date=BASE_DAY,
        instruments=[instrument],
        latest={
            1: (
                candle(close=105.0, offset_minutes=10),
                snap(candle_start=minute(10), warming_up=True),
                True,
                False,
                (),
            )
        },
        now=minute(10),
    )
    assert summary["counts"]["warming_up"] == 1
    assert summary["counts"]["qualified"] == 0
    assert summary["ranked"] == []
    assert manager.store.memberships == {}


# ── §12 end of day ───────────────────────────────────────────────────────────


async def test_finalize_day_freezes_every_row_and_reports_stale(manager):
    # §12 every row ends is_active False; a row still parked in DATA_STALE at
    # close is reported as unresolved.
    store = manager.store
    await feed(manager, inst(id=1), snap(candle_start=minute(5)), offset=5, bullish=True)
    for offset in (6, 7, 8):
        await feed(
            manager,
            inst(id=1),
            removal_snap(instrument_id=1, candle_start=minute(offset)),
            offset=offset,
            bullish=False,
        )
    await feed(
        manager, inst(id=2, symbol="TATAMOTORS"), snap(instrument_id=2, candle_start=minute(5)),
        offset=5, bullish=True,
    )
    await feed(
        manager, inst(id=3, symbol="INFY"), snap(instrument_id=3, candle_start=minute(5)),
        offset=5, bullish=True,
    )
    await feed(
        manager,
        inst(id=3, symbol="INFY"),
        snap(instrument_id=3, candle_start=minute(6)),
        offset=6,
        bullish=True,
        data_quality=DataQuality.STALE,
    )
    stale_id = row(store, 3).id
    assert row(store, 1).is_active is False  # already removed pre-close

    report = await manager.finalize_day(session_id=1, now=minute(200))
    assert report["kind"] == "end_of_day"
    assert report["frozen_rows"] == 2  # the ACTIVE row + the parked DATA_STALE row
    assert report["total_rows"] == 3
    assert report["unresolved_stale_memberships"] == [stale_id]
    assert all(not membership.is_active for membership in store.memberships.values())


# ── §4 re-entry through the alert engine ────────────────────────────────────


async def test_requalification_allows_fresh_bullish_entry_signal(alert_manager):
    # §4/§6 re-entry after removal gets a NEW episode id, so BULLISH_ENTRY can
    # be raised again (its idempotency key changed).
    store = alert_manager.store
    instrument = inst()
    added = await feed(
        alert_manager, instrument, snap(candle_start=minute(5)), offset=5, bullish=True
    )
    assert added.signaled is True
    for offset in (6, 7, 8):
        await feed(
            alert_manager, instrument, removal_snap(candle_start=minute(offset)), offset=offset, bullish=False
        )

    # 25 minutes later: the 15-minute cooldown has lapsed, so the fresh
    # episode's entry alert is genuinely a new creation.
    requalified = await feed(
        alert_manager, instrument, snap(candle_start=minute(30)), offset=30, bullish=True
    )
    assert requalified.state is WatchlistState.REQUALIFIED
    assert requalified.signaled is True

    keys = set(store.signal_by_key)
    assert "WATCHLIST_ADDED:1:2026-10-12-1-e1" in keys
    assert "BULLISH_ENTRY:1:2026-10-12-1-e1" in keys
    assert "WATCHLIST_REMOVED:1:2026-10-12-1-e1" in keys
    assert "WATCHLIST_REQUALIFIED:1:2026-10-12-1-e2" in keys
    assert "BULLISH_ENTRY:1:2026-10-12-1-e2" in keys
    assert len(keys) == 5
