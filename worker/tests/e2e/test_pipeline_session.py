"""End-to-end all-day pipeline session tests.

Each test builds a real :class:`ScannerPipeline` on an ``InMemoryStore`` with
synthetic NSE instruments, a :class:`CandleEngine` bound to the 09:15-15:30 IST
window from ``tests/conftest``, and drives it minute by minute with
``on_tick`` + ``drain(minute + 4s)``. No network, no wall clock, no sleeping.

Pinned spec sections: §8 (candle build), §9 (session VWAP / EMA / RV), §10-§11
(qualification, watchlist state machine, initial scan), §12.3 (restart
idempotency), §12.4 (outage episodes), §12.5 (queued deliveries), §17 (heartbeat,
close/finalize).

Configuration note: sessions are built with ``alert_cooldown_minutes=0`` so
every episode raised during a run produces its queued delivery row; the
default 15-minute window semantics (first push through, repeats suppressed)
are pinned in tests/unit/test_alerts.py and
tests/unit/test_dispatcher.py.

The level-break detectors (day high / ORB / VWAP) are muted for every session
here — see :func:`build`. Armed behaviour is :mod:`tests.e2e.test_breakout_session`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta

import pytest

from tests.conftest import BASE_DAY, SESSION_CLOSE, SESSION_OPEN, minute, tick
from worker.alerts import AlertEngine
from worker.analytics.breakouts import DEFAULTS, STRATEGIES, StrategiesConfig
from worker.analytics.indicators import IndicatorRegistry
from worker.analytics.scanner import F_INSUFFICIENT_HISTORY
from worker.config import ScannerParams
from worker.marketdata.candle_builder import CandleEngine
from worker.models import (
    Channel,
    DataQuality,
    DeliveryStatus,
    FeedStatus,
    Instrument,
    SignalType,
    WatchlistState,
)
from worker.persistence.memory import InMemoryStore
from worker.pipeline import ScannerPipeline
from worker.timeutils import TradingCalendar
from worker.watchlist import WatchlistManager

CHAT = "chat-1"

# A weekday-only calendar with the documented NSE hours: 09:15 open, 15:30 close.
CALENDAR = TradingCalendar(
    holidays=frozenset(),
    holiday_reasons={},
    open_time=time(9, 15),
    close_time=time(15, 30),
    pre_open_start=time(9, 0),
    post_close_end=time(16, 0),
    source="test",
    source_type="test",
    retrieved_at=None,
    loaded_from_file=False,
    stale=False,
)
WINDOW = CALENDAR.session(BASE_DAY)


def all_strategies_muted() -> StrategiesConfig:
    """Every level-break detector switched off, thresholds left at their defaults.

    Exported for :mod:`tests.e2e.test_breakout_session`, which builds the same
    session with the detectors armed instead.
    """
    return StrategiesConfig(
        **{key: replace(DEFAULTS[key], enabled=False) for key in STRATEGIES}
    )


def inst(iid: int = 1, symbol: str = "RELIANCE", previous_close: float = 100.0) -> Instrument:
    return Instrument(
        id=iid,
        exchange="NSE",
        symbol=symbol,
        trading_symbol=symbol,
        company_name=f"{symbol} Test Ltd",
        instrument_token=str(2885 + iid),  # distinct instrument tokens
        previous_close=previous_close,
    )


def rising_ticks(
    minutes: int = 25,
    *,
    start: float = 100.0,
    step: float = 0.5,
    volume: int = 1000,
    spike_minute: int | None = 22,
    spike_volume: int = 6000,
) -> list[tuple[float, float, int]]:
    """(open, close, minute-volume) per minute: a steadily climbing price line.

    A volume spike at ``spike_minute`` is what pushes relative volume over the
    1.5x threshold once the indicators are warm (candles_seen >= 21).
    """
    rows = []
    for m in range(minutes):
        price_open = start + step * m
        vol = spike_volume if (spike_minute is not None and m == spike_minute) else volume
        rows.append((price_open, price_open + 0.2, vol))
    return rows


def falling_ticks(minutes: int = 25, *, start: float = 50.0, step: float = 0.05) -> list[tuple[float, float, int]]:
    return [(start - step * m, start - step * m - 0.02, 1000) for m in range(minutes)]


class Session:
    """Pipeline + deterministic tick plumbing for one synthetic trading day."""

    def __init__(self, pipeline: ScannerPipeline, session_row, plans: dict[int, list[tuple[float, float, int]]]):
        self.pipeline = pipeline
        self.session_row = session_row
        self.store: InMemoryStore = pipeline.store
        self.plans = plans
        self.cum: dict[int, int] = dict.fromkeys(pipeline.instruments, 0)
        self.next_minute = 0

    async def connect(self) -> None:
        # §7.2: the pipeline starts DISCONNECTED and refuses watchlist work
        # until the feed reports STREAMING — every healthy test opens with this.
        await self.pipeline.on_feed_status(FeedStatus.STREAMING, "test frames flowing", now=minute(0, second=1))

    async def feed_minute(self, mi: int) -> None:
        for iid, rows in self.plans.items():
            if mi >= len(rows):
                continue
            price_open, price_close, vol = rows[mi]
            self.cum[iid] += vol
            token = self.pipeline.instruments[iid].instrument_token
            await self.pipeline.on_tick(
                tick(instrument_id=iid, token=token, price=price_open, offset_minutes=mi, second=0,
                     cumulative_volume=self.cum[iid])
            )
            await self.pipeline.on_tick(
                tick(instrument_id=iid, token=token, price=price_close, offset_minutes=mi, second=30,
                     cumulative_volume=self.cum[iid])
            )
        self.next_minute = max(self.next_minute, mi + 1)

    async def run_minutes(self, count: int) -> None:
        for _ in range(count):
            mi = self.next_minute
            await self.feed_minute(mi)
            # boundary (minute+1) + grace 3s: draining at +4s finalizes exactly
            # the one due minute (§7.3: processed by time, never by a later tick)
            await self.pipeline.drain(minute(mi + 1, second=4))

    async def stale(self, reason: str, now) -> None:
        await self.pipeline.on_feed_status(FeedStatus.STALE, reason, now=now)

    async def recover(self, now) -> None:
        await self.pipeline.on_feed_status(FeedStatus.STREAMING, "frames resumed", now=now)

    def signals_of(self, kind: SignalType, instrument_id: int | None = None) -> list:
        return [
            s
            for s in self.store.signals.values()
            if s.signal_type is kind and (instrument_id is None or s.instrument_id == instrument_id)
        ]

    def deliveries_for(self, channel: Channel) -> list:
        return [d for d in self.store.deliveries.values() if d.channel is channel]


async def build(
    instruments: list[Instrument],
    plans: dict[int, list[tuple[float, float, int]]],
    *,
    simulation: bool = True,
    telegram: bool = False,
    cooldown: int = 0,
    strategies: StrategiesConfig | None = None,
    store: InMemoryStore | None = None,
) -> Session:
    """One synthetic trading day.

    ``strategies=None`` means every level-break detector is *muted* here, which is
    not the shipped configuration (that arms all three). This file's assertions are
    hand-computed counts of watchlist signals and queued deliveries, and a breakout
    row added to each tape would turn every one of them into arithmetic unrelated to
    what the test pins. The detectors run armed in
    :mod:`tests.e2e.test_breakout_session`.

    ``store`` is passed in by a restart test that wants the second process to see
    the candles the first one wrote.
    """
    params = ScannerParams(alert_cooldown_minutes=cooldown)
    armed = strategies if strategies is not None else all_strategies_muted()
    if store is None:
        store = InMemoryStore()
    await store.upsert_instruments(instruments)
    session = await store.ensure_session(BASE_DAY, SESSION_OPEN, SESSION_CLOSE)
    await store.set_session_status(session.id, "IN_PROGRESS")
    alerts = AlertEngine(
        store=store,
        params=params,
        telegram_enabled=telegram,
        telegram_chat_id=CHAT if telegram else "",
        simulation=simulation,
    )
    pipeline = ScannerPipeline(
        store=store,
        params=params,
        calendar=CALENDAR,
        session=session,
        window=WINDOW,
        instruments={item.id: item for item in instruments},
        engine=CandleEngine(
            grace_seconds=params.candle_grace_seconds,
            session_open=SESSION_OPEN,
            session_close=SESSION_CLOSE,
        ),
        registry=IndicatorRegistry(
            fast_period=params.fast_ema_period,
            slow_period=params.slow_ema_period,
            volume_lookback=params.volume_lookback,
        ),
        watchlist=WatchlistManager(store=store, params=params, alerts=alerts, simulation=simulation),
        alerts=alerts,
        is_simulated=simulation,
        strategies=armed,
    )
    return Session(pipeline, session, plans)


# ── §8: candle construction from the tick stream ─────────────────────────────


async def test_exactly_one_candle_per_instrument_per_minute_derived_from_ticks():
    # spec §8: OHLCV comes from the ticks, one finalized row per minute bucket,
    # and re-draining the same minute must not duplicate anything.
    rise, fall = inst(1, "RELIANCE"), inst(2, "HDFCBANK", previous_close=20.0)
    s = await build([rise, fall], {1: rising_ticks(minutes=6), 2: falling_ticks(minutes=6, start=20.0)})
    await s.connect()
    await s.run_minutes(6)

    for iid, plan in s.plans.items():
        rows = s.store.candle_rows(iid)
        assert len(rows) == 6, f"instrument {iid} must have exactly one candle per fed minute"
        for m, candle in enumerate(rows):
            po, pc, vol = plan[m]
            assert candle.candle_start == minute(m)
            assert candle.trading_date == BASE_DAY
            # Both sides go through the same 4-decimal rounding the store applies
            # (NUMERIC(16,4)); the tick plan itself is binary-float arithmetic, so
            # ``20.0 - 0.05*3 - 0.02`` arrives as 19.830000000000002.
            expected = tuple(round(value, 4) for value in (po, max(po, pc), min(po, pc), pc))
            assert (candle.open, candle.high, candle.low, candle.close) == expected
            assert candle.volume == vol, "volume is the delta of the day-cumulative counter"
            assert candle.is_complete is True
            assert candle.data_quality is DataQuality.OK

    # draining again at the same instant finalizes nothing new (§7.3)
    await s.pipeline.drain(minute(6, second=4))
    assert len(s.store.candle_rows(1)) == 6, "re-draining the same minute must not duplicate candles"


# ── §9: indicator snapshots on every finalized candle ────────────────────────


async def test_snapshot_per_candle_with_hand_computed_session_vwap_and_ema():
    # spec §9: session VWAP = Σ TP·V / Σ V over this session's candles only;
    # EMA is SMA-seeded (period 9 initialises on the 9th candle, not before).
    s = await build([inst(1)], {1: rising_ticks(minutes=10)})
    await s.connect()
    await s.run_minutes(10)

    plan = s.plans[1]
    assert len(s.store.snapshots) == 10, "every finalized candle carries one indicator snapshot"
    for m in range(10):
        assert (1, minute(m)) in s.store.snapshots, f"snapshot missing for minute {m}"

    # equal volumes -> VWAP is the plain mean of typical prices
    typical = [( (po + 2 * pc) / 3.0 ) for po, pc, _v in plan]  # H=pc, L=po, C=pc
    expected_vwap = sum(typical) / len(typical)
    last = s.store.snapshots[(1, minute(9))]
    assert last.vwap == pytest.approx(expected_vwap, rel=1e-9)
    assert last.close == plan[-1][1]

    fast_at_seed = s.store.snapshots[(1, minute(8))]  # 9th candle seeds EMA9
    assert fast_at_seed.ema_fast == pytest.approx(sum(pc for _po, pc, _v in plan[:9]) / 9.0, rel=1e-9)
    assert fast_at_seed.ema_slow is None, "EMA20 cannot exist on candle 9"
    assert s.store.snapshots[(1, minute(7))].ema_fast is None, "EMA9 must be None before its seed"


async def test_nothing_is_evaluable_before_warmup_and_warmup_report_shrinks():
    # spec §8.4/§10: warm-up gates evaluation; nothing may qualify while
    # candles_seen < warmup_candles_required (20) or indicators are not warm.
    params = ScannerParams()
    rise, fall = inst(1, "RELIANCE"), inst(2, "ITC", previous_close=50.0)
    s = await build([rise, fall], {1: rising_ticks(minutes=22), 2: falling_ticks(minutes=22)})
    await s.connect()

    report0 = s.pipeline.warmup_report()
    assert report0["required_candles"] == params.warmup_candles_required == 20
    assert report0["warming_up"] == 2 and report0["ready"] == 0

    await s.run_minutes(5)
    assert s.pipeline.warmup_report()["warming_up"] == 2, "5 candles < 20"

    await s.run_minutes(15)  # 20 candles now: EMA20 seeded, but RV needs 21 prints
    assert s.pipeline.warmup_report() == {
        "required_candles": 20,
        "ready": 2,
        "warming_up": 0,
        "with_any_history": 2,
    }
    for m in range(20):
        snap = s.store.snapshots[(1, minute(m))]
        assert snap.warming_up is True or m < 20
        assert F_INSUFFICIENT_HISTORY in snap.failed_reasons, f"candle {m} (< warm-up) must be unaevaluable"
        assert snap.bullish_qualified is False
    assert s.store.signals == {}, "no alert may be minted before warm-up"

    await s.run_minutes(1)  # 21st candle: all indicators warm
    warm_snap = s.store.snapshots[(1, minute(20))]
    assert F_INSUFFICIENT_HISTORY not in warm_snap.failed_reasons
    assert warm_snap.warming_up is False
    _c, _snap, _bullish, evaluable, _reasons = s.pipeline.latest_evaluations()[1]
    assert evaluable is True, "21 session candles >= warmup_candles_required 20 becomes evaluable"


# ── §10/§11: qualification, watchlist and entry alerts ───────────────────────


async def test_rising_high_volume_instrument_reaches_active_watchlist_and_mints_entry():
    # spec §10.1 + §11.2: close > VWAP, EMA9 > EMA20 and RV >= 1.5 together add
    # the stock to the watchlist (ACTIVE) and mint exactly one BULLISH_ENTRY.
    rise, fall = inst(1, "RELIANCE"), inst(2, "TATASTEEL", previous_close=50.0)
    s = await build([rise, fall], {1: rising_ticks(minutes=25), 2: falling_ticks(minutes=25)})
    await s.connect()
    await s.run_minutes(25)

    membership = await s.store.get_membership(s.session_row.id, 1)
    assert membership is not None, "the rising spike-volume stock must be on the watchlist"
    assert membership.current_state is WatchlistState.ACTIVE
    assert membership.latest_score is not None and membership.latest_score > 0

    entries = s.signals_of(SignalType.BULLISH_ENTRY, instrument_id=1)
    assert len(entries) == 1, "one entry alert per qualifying episode (§12.1)"
    assert entries[0].qualifying_episode_id == f"{BASE_DAY.isoformat()}-1-e1"
    assert entries[0].candle_start == minute(22), "the RV spike candle is the qualifying candle"
    added = s.signals_of(SignalType.WATCHLIST_ADDED, instrument_id=1)
    assert len(added) == 1

    # the flat/declining stock produces nothing at all
    assert s.signals_of(SignalType.BULLISH_ENTRY, instrument_id=2) == []
    assert s.signals_of(SignalType.WATCHLIST_ADDED, instrument_id=2) == []
    assert await s.store.get_membership(s.session_row.id, 2) is None
    assert s.pipeline.counters.signals_raised == 1


async def test_double_evaluation_after_restart_mints_no_duplicate_signal_or_queue_row():
    # spec §12.3: reprocessing the same finalized candle (crash/restart replay)
    # must not add a second entry signal nor a second queued delivery row.
    # This is the telegram-enabled scenario of the all-day pipeline.
    rise, fall = inst(1, "RELIANCE"), inst(2, "WIPRO", previous_close=50.0)
    s = await build([rise, fall], {1: rising_ticks(minutes=25), 2: falling_ticks(minutes=25)},
                    simulation=False, telegram=True)
    await s.connect()
    await s.run_minutes(25)

    entry_candle = s.store.candle_rows(1)[22]
    assert entry_candle.candle_start == minute(22)

    signals_before = len(s.store.signals)
    telegram_before = s.deliveries_for(Channel.TELEGRAM)
    dashboard_before = s.deliveries_for(Channel.DASHBOARD)
    assert signals_before == 2, "WATCHLIST_ADDED + BULLISH_ENTRY expected from one entry episode"
    assert all(d.delivery_status is DeliveryStatus.QUEUED for d in telegram_before)
    snapshot_id_before = s.store.snapshot_ids[(1, minute(22))]
    candles_before = len(s.store.candle_rows(1))

    replay = [entry_candle]
    await s.pipeline.process_candles(replay, now=minute(26, second=4))
    await s.pipeline.process_candles(replay, now=minute(26, second=4))

    assert len(s.store.signals) == signals_before, "the episode id in the key must block a second entry"
    assert s.signals_of(SignalType.BULLISH_ENTRY, instrument_id=1) == [
        next(sg for sg in s.store.signals.values() if sg.signal_type is SignalType.BULLISH_ENTRY)
    ]
    assert len(s.deliveries_for(Channel.TELEGRAM)) == len(telegram_before)
    assert len(s.deliveries_for(Channel.DASHBOARD)) == len(dashboard_before)
    assert len(s.store.candle_rows(1)) == candles_before, "candle upsert keyed by (instrument, minute)"
    assert len(s.store.snapshots) == 50, "snapshot upsert keyed by (instrument, minute)"
    assert s.store.snapshot_ids[(1, minute(22))] == snapshot_id_before
    membership = await s.store.get_membership(s.session_row.id, 1)
    assert membership.current_state is WatchlistState.ACTIVE, "replay must not re-enter or remove"


# ── §7.2 / §12.4: feed outage behaviour ──────────────────────────────────────


async def test_outage_stores_data_but_freezes_membership_with_exactly_one_interrupted_alert():
    # spec §7.2: a stale feed still persists candle + snapshot, but membership
    # state and signals are frozen; §12.4: one interrupt alert per episode.
    s = await build([inst(1, "RELIANCE")], {1: rising_ticks(minutes=26)})
    await s.connect()
    await s.run_minutes(25)

    membership = await s.store.get_membership(s.session_row.id, 1)
    state_before, score_before, price_before = (
        membership.current_state,
        membership.latest_score,
        membership.latest_price,
    )
    signals_before = len(s.store.signals)

    outage_start = minute(25, second=20)
    await s.stale("no frames", now=outage_start)
    assert s.signals_of(SignalType.DATA_FEED_INTERRUPTED) and s.pipeline.counters.outages == 1

    await s.run_minutes(1)  # minute 25 is finalized *during* the outage
    rows = s.store.candle_rows(1)
    assert len(rows) == 26, "candles keep being stored while the feed is stale"
    assert (1, minute(25)) in s.store.snapshots, "snapshots are stored too"
    after = await s.store.get_membership(s.session_row.id, 1)
    assert after.current_state is state_before, "membership state is frozen during an outage (§7.2)"
    assert after.latest_score == score_before and after.latest_price == price_before
    assert len(s.store.signals) == signals_before + 1, "the only new signal may be the outage alert"
    assert len(s.signals_of(SignalType.BULLISH_ENTRY)) == 1, "no entry minted while stale"

    # a second STALE report inside the same episode does not alert again
    await s.stale("still no frames", now=minute(26, second=10))
    assert len(s.signals_of(SignalType.DATA_FEED_INTERRUPTED)) == 1
    assert s.pipeline.counters.outages == 1, "repeated STALE frames are one episode"


async def test_recovery_raises_one_recovered_alert_and_accumulates_stale_seconds():
    # §12.4: recovery closes the episode exactly once and the durable session
    # row carries the accumulated stale seconds for the day.
    s = await build([inst(1, "RELIANCE")], {1: rising_ticks(minutes=26)})
    await s.connect()
    await s.run_minutes(25)

    outage_start = minute(25, second=20)
    await s.stale("no frames", now=outage_start)
    await s.recover(now=outage_start + timedelta(seconds=60))

    assert len(s.signals_of(SignalType.DATA_FEED_RECOVERED)) == 1
    assert len(s.signals_of(SignalType.DATA_FEED_INTERRUPTED)) == 1
    row = s.store.sessions[s.session_row.id]
    assert row.stale_data_seconds == 60, "quiet_seconds must be accumulated on the session row"
    assert row.stale_data_seconds > 0
    assert s.pipeline.counters.stale_seconds_total == pytest.approx(60.0)
    assert s.pipeline._outage is None, "the episode must be closed after recovery"

    # healthy processing resumes: the next minute reaches the watchlist again
    await s.run_minutes(1)
    assert len(s.store.candle_rows(1)) == 26
    membership = await s.store.get_membership(s.session_row.id, 1)
    assert membership.current_state is WatchlistState.ACTIVE


async def test_second_outage_episode_raises_its_own_interrupted_and_recovered_alerts():
    # §12.4: outage episodes are the dedupe unit — two episodes mean one
    # interrupt+recover pair each, keyed by the episode id.
    s = await build([inst(1, "RELIANCE")], {1: rising_ticks(minutes=26)})
    await s.connect()
    await s.run_minutes(25)

    await s.stale("first dip", now=minute(25, second=20))
    await s.recover(now=minute(26, second=20))
    await s.stale("second dip", now=minute(27, second=20))
    await s.recover(now=minute(28, second=20))

    assert s.pipeline.counters.outages == 2
    interrupted = s.signals_of(SignalType.DATA_FEED_INTERRUPTED)
    recovered = s.signals_of(SignalType.DATA_FEED_RECOVERED)
    assert len(interrupted) == 2, "the second episode must raise its own interruption alert"
    assert len(recovered) == 2, "and its own recovery alert"
    assert {sig.qualifying_episode_id for sig in interrupted} == {
        f"outage-{s.session_row.id}-1",
        f"outage-{s.session_row.id}-2",
    }


# ── §17 item 15: heartbeat honesty ───────────────────────────────────────────


async def test_heartbeat_reports_only_observed_progress():
    rise, climb, fall = inst(1, "RELIANCE"), inst(2, "BAJFINANCE"), inst(3, "IOC", previous_close=50.0)
    s = await build(
        [rise, climb, fall],
        {
            1: rising_ticks(minutes=25),
            # steadily rising but flat volume: never passes the RV gate
            2: rising_ticks(minutes=25, step=0.2, spike_minute=None),
            3: falling_ticks(minutes=25),
        },
        simulation=False,
        telegram=True,
    )

    await s.pipeline.publish_heartbeat(
        worker_id="w1", now=minute(0), subscribed_count=3, last_market_data_at=None
    )
    hb = s.store.heartbeats["w1"]
    assert hb.last_completed_candle_at is None, "no candle has completed yet — the heartbeat must say so"
    assert hb.universe_total == 3 and hb.mapped_total == 3
    assert hb.monitored_total == 0 and hb.subscribed_instrument_count == 3
    assert hb.active_watchlist_count == 0 and hb.pending_notification_count == 0
    # §17 honesty: this pipeline was built with simulation=False, so the row
    # must claim live — and the reverse must also hold (see the next test).
    assert hb.scan_mode == "live" and hb.simulation_label is False

    await s.connect()
    await s.run_minutes(25)
    await s.pipeline.publish_heartbeat(
        worker_id="w1", now=minute(25), subscribed_count=3, last_market_data_at=None
    )
    hb = s.store.heartbeats["w1"]
    assert hb.last_completed_candle_at == minute(24), "newest in-session finalized candle start"
    assert hb.monitored_total == 3, "all three instruments actually received ticks"
    assert hb.active_watchlist_count == 1, "only the spike-volume riser is ACTIVE/WEAKENING"
    expected_pending = await s.store.pending_notification_count()
    assert hb.pending_notification_count == expected_pending == 2, "ADDED + ENTRY queued for Telegram"
    assert hb.queue_depth == expected_pending
    assert hb.feed_status is FeedStatus.STREAMING
    assert hb.last_market_data_at == minute(24, second=30), (
        "fallback is the newest exchange stamp actually observed"
    )


async def test_heartbeat_labels_simulation_runs_as_simulated():
    # A simulated worker must never report itself live on the operator dashboard
    # (spec §17: the SimChip and scan_mode cell read these columns directly).
    rise = inst(1, "RELIANCE")
    s = await build([rise], {1: rising_ticks(minutes=2)})  # simulation=True default
    await s.pipeline.publish_heartbeat(
        worker_id="w1", now=minute(0), subscribed_count=1, last_market_data_at=None
    )
    hb = s.store.heartbeats["w1"]
    assert hb.scan_mode == "simulation"
    assert hb.simulation_label is True


# ── §11.1: initial scan ──────────────────────────────────────────────────────


async def test_run_initial_scan_marks_session_writes_alert_and_summary_idempotently():
    rise, fall = inst(1, "RELIANCE"), inst(2, "IOC", previous_close=50.0)
    s = await build([rise, fall], {1: rising_ticks(minutes=25), 2: falling_ticks(minutes=25)})
    await s.connect()
    await s.run_minutes(25)  # the riser already entered live; scan sees no fresh qualifiers

    now = minute(26, second=10)
    summary = await s.pipeline.run_initial_scan(now)
    assert summary["kind"] == "initial_scan"
    assert summary["universe"] == 2 and summary["counts"]["evaluated"] == 2
    assert summary["counts"]["missing_data"] == 0
    assert s.pipeline.initial_scan_completed is True
    assert s.store.sessions[s.session_row.id].initial_scan_completed_at == now
    assert len(s.signals_of(SignalType.DAILY_SCAN_COMPLETE)) == 1
    assert BASE_DAY in s.store.summaries
    assert s.store.summaries[BASE_DAY]["kind"] == "initial_scan"

    signals_before = len(s.store.signals)
    deliveries_before = len(s.store.deliveries)
    await s.pipeline.run_initial_scan(minute(27, second=10))
    assert len(s.store.signals) == signals_before, (
        "the session-scoped idempotency key must fold the second scan into the first"
    )
    assert len(s.store.deliveries) == deliveries_before, "a duplicate scan queues no new notifications"


async def test_initial_scan_refuses_to_run_before_the_configured_candle_count():
    # §11.1: initial_scan_candles = 5 — fewer session candles than that and the
    # scan is skipped without touching the completion marker.
    s = await build([inst(1, "RELIANCE")], {1: rising_ticks(minutes=3)})
    await s.connect()
    await s.run_minutes(3)
    result = await s.pipeline.run_initial_scan(minute(4))
    assert result.get("skipped"), f"3 candles < initial_scan_candles 5 must skip, got {result!r}"
    assert s.pipeline.initial_scan_completed is False
    assert s.store.sessions[s.session_row.id].initial_scan_completed_at is None
    assert s.signals_of(SignalType.DAILY_SCAN_COMPLETE) == []


async def test_initial_scan_creates_the_first_watchlist_ranked_by_score():
    # §11.1: the initial scan evaluates the whole mapped universe once, adds
    # every qualifier and orders them by score (strongest first).
    # Driven while the feed is still DISCONNECTED so the live path has not
    # pre-created any membership: the scan must build the day's first watchlist.
    strong = inst(1, "STRONG")
    climb = inst(2, "CLIMB")
    flat = inst(3, "FLATCO", previous_close=50.0)
    s = await build(
        [strong, climb, flat],
        {
            1: rising_ticks(minutes=23),  # spike RV 6.0x at minute 22
            2: rising_ticks(minutes=23, start=100.0, step=0.2, spike_volume=1800),  # RV 1.8x, weaker move
            3: falling_ticks(minutes=23),
        },
    )
    await s.run_minutes(23)
    assert s.store.memberships == {}, "disconnected feed must have suspended live membership work"

    summary = await s.pipeline.run_initial_scan(minute(23, second=10))
    assert summary["counts"]["qualified"] == 2
    assert summary["ranked"] == ["STRONG", "CLIMB"], "qualifiers must be ordered by score, best first"
    assert summary["counts"]["not_qualified"] == 1
    for instrument_id in (1, 2):
        membership = await s.store.get_membership(s.session_row.id, instrument_id)
        assert membership is not None and membership.current_state is WatchlistState.ACTIVE


# ── close of session: flush + finalize (§11.8) ───────────────────────────────


async def test_flush_session_finalizes_the_forming_minute_at_close():
    s = await build([inst(1, "RELIANCE")], {1: rising_ticks(minutes=27)})
    await s.connect()
    await s.run_minutes(26)
    assert len(s.store.candle_rows(1)) == 26

    # minute 26 has arrived but its boundary+grace has NOT passed: only the
    # close flush may finalize it (§8: never invent, never finalize early).
    await s.feed_minute(26)

    flushed = await s.pipeline.flush_session(minute(26, second=50))
    assert [(c.instrument_id, c.candle_start) for c in flushed] == [(1, minute(26))]
    rows = s.store.candle_rows(1)
    assert len(rows) == 27 and rows[-1].candle_start == minute(26)
    assert rows[-1].is_complete is True


async def test_finalize_day_freezes_watchlist_and_returns_closing_payload():
    rise, fall = inst(1, "RELIANCE"), inst(2, "IOC", previous_close=50.0)
    s = await build([rise, fall], {1: rising_ticks(minutes=27), 2: falling_ticks(minutes=27)})
    await s.connect()
    await s.run_minutes(25)

    outage_start = minute(25, second=20)
    await s.stale("no frames", now=outage_start)
    await s.recover(now=outage_start + timedelta(seconds=90))
    await s.run_minutes(2)  # minutes 25 + 26 processed again under a healthy feed

    payload = await s.pipeline.finalize_day(minute(27, second=10))
    assert payload["kind"] == "daily_summary"
    assert payload["trading_date"] == BASE_DAY.isoformat()
    assert payload["stale_seconds"] == pytest.approx(90.0), "observed outage time must be reported (§12.4)"

    watch = payload["final_watchlist"]
    assert [w["symbol"] for w in watch] == ["RELIANCE"], "only instruments with memberships are ranked"
    assert watch[0]["state"] == WatchlistState.ACTIVE.value
    assert watch[0]["rank"] == 1 and watch[0]["score"] is not None

    session_row = s.store.sessions[s.session_row.id]
    assert session_row.session_status == "FINALIZED"
    assert session_row.finalized_at == minute(27, second=10)
    membership = await s.store.get_membership(s.session_row.id, 1)
    assert membership.is_active is False, "the day's watchlist is frozen (§11.8)"
    assert membership.current_state is WatchlistState.ACTIVE, "frozen does not mean removed"
    assert await s.store.get_membership(s.session_row.id, 2) is None, "the flat stock never joined"
    assert s.store.summaries[BASE_DAY]["kind"] == "daily_summary"
