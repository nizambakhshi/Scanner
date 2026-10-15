"""Level-break detectors running inside a real pipeline session.

The unit tests in :mod:`tests.unit.test_breakouts` pin the detector's arithmetic
against synthetic candles. This file pins the part only an end-to-end run can
show: a finalized minute reaching the tracker, the resulting event becoming a
signal row with the right identity, and that signal becoming the right delivery
rows — including the two things the operator asked for on 2026-10-14, a
per-strategy Telegram switch and a mid-day restart that must not re-page the
phone.

These sessions arm the detectors with the shipped defaults and drop the ORB/VWAP
volume gate to ``0`` where a tape shorter than the 21-candle relative-volume
baseline would otherwise silence them; the gate itself is pinned in the unit file.
"""

from __future__ import annotations

from dataclasses import replace

from tests.conftest import BASE_DAY, minute
from tests.e2e.test_pipeline_session import build, falling_ticks, inst, rising_ticks
from worker.analytics.breakouts import DEFAULTS, StrategiesConfig
from worker.models import Channel, DeliveryStatus, SignalType


def session_params(**changes: object) -> StrategiesConfig:
    """The shipped defaults with a few fields changed, per strategy name.

    A bare boolean switches that strategy off; a mapping is applied onto its
    defaults. ``session_break={"min_volume_ratio": 0.0}`` removes the volume gate
    so the break is judged on price alone — relative volume has no 21-candle
    baseline in a 24-minute tape, and the gate is not what these tests are about.
    """
    values = {key: DEFAULTS[key] for key in DEFAULTS}
    for name, patch in changes.items():
        if isinstance(patch, bool):
            values[name] = replace(values[name], enabled=patch)
        else:
            values[name] = replace(values[name], **(patch or {}))
    return StrategiesConfig(**values)  # type: ignore[arg-type]


#: The day-break rule alone, judged on price without the volume gate: one clean
#: BREAKOUT_UP per rising tape, which is what the identity and delivery tests need.
DAY_BREAK: dict[str, object] = {
    "session_break": {"min_volume_ratio": 0.0},
    "orb": False,
    "vwap": False,
}
#: Same rule, same break, no phone ping — the operator's per-strategy Telegram switch.
DAY_BREAK_BOARD_ONLY: dict[str, object] = {
    "session_break": {"min_volume_ratio": 0.0, "telegram": False},
    "orb": False,
    "vwap": False,
}


def orb_retest_ticks() -> list[tuple[float, float, int]]:
    """13 minutes planned around the 5-minute ORB buckets and the setup levels.

    * minutes 0-4 (bucket 0) print 100.0→100.2: the opening range, high 100.2;
    * minutes 5-9 (bucket 1) rally to a close of 101.8 — judged, and the range
      broken, when minute 10 opens bucket 2 (the event is stamped minute 9);
    * minute 11 dips to 100.1: inside the 0.5% band top 100.701, above the
      failure line 99.699, closing below the level — a touch, not a reclaim;
    * minute 12 closes 100.6, above the level: the reclaim.
    """
    flat = [(100.0, 100.2, 1000)] * 5
    rally = [
        (100.4, 100.6, 1000),
        (100.6, 100.9, 1000),
        (100.9, 101.2, 1000),
        (101.2, 101.5, 1000),
        (101.5, 101.8, 1000),
    ]
    retest = [(101.6, 101.3, 1000), (101.0, 100.1, 1000), (100.3, 100.6, 1000)]
    return flat + rally + retest


async def test_a_new_day_high_becomes_a_breakout_signal_and_a_telegram_push():
    rise, fall = inst(1, "RELIANCE"), inst(2, "TATASTEEL", previous_close=50.0)
    s = await build(
        [rise, fall],
        {1: rising_ticks(minutes=24), 2: falling_ticks(minutes=24)},
        simulation=False,
        telegram=True,
        strategies=session_params(**DAY_BREAK),
    )
    await s.connect()
    await s.run_minutes(24)

    ups = s.signals_of(SignalType.BREAKOUT_UP, instrument_id=1)
    downs = s.signals_of(SignalType.BREAKDOWN, instrument_id=2)
    assert ups, "a steadily climbing tape must break the day high"
    assert downs, "and a steadily falling one the day low"
    assert s.signals_of(SignalType.BREAKDOWN, instrument_id=1) == []
    assert s.signals_of(SignalType.BREAKOUT_UP, instrument_id=2) == []

    signal = ups[0]
    # The strategy travels in reasons[0]: two signal types carry three strategies,
    # and this is the only field that survives into the database to tell them apart.
    assert signal.reasons[0] == "strategy:session_break"
    assert "day_high_break" in signal.reasons
    assert signal.message is not None and "broke the day high" in signal.message
    assert signal.price is not None and signal.price > 100.0
    # The episode identity is level-keyed: same day, stock, strategy, direction and
    # level — which is what makes a restart replay a no-op rather than a second alert.
    assert signal.qualifying_episode_id.startswith(
        f"{BASE_DAY.isoformat()}-1-session_break-UP-"
    )
    assert signal.intraday_change_pct is not None, "the previous close reached the alert"

    channels = {
        d.channel: d.delivery_status for d in s.store.deliveries.values() if d.signal_id == signal.id
    }
    assert channels == {
        Channel.DASHBOARD: DeliveryStatus.DELIVERED,
        Channel.TELEGRAM: DeliveryStatus.QUEUED,
    }, "an armed strategy with Telegram on reaches the board and queues the phone"
    assert s.pipeline.counters.breakouts_detected == len(ups) + len(downs)
    assert s.pipeline.counters.breakouts_alerted == len(ups) + len(downs)


async def test_reprocessing_the_same_candle_mints_no_second_breakout_row():
    s = await build(
        [inst(1, "RELIANCE")],
        {1: rising_ticks(minutes=24)},
        strategies=session_params(**DAY_BREAK),
    )
    await s.connect()
    await s.run_minutes(24)

    ups = s.signals_of(SignalType.BREAKOUT_UP, instrument_id=1)
    assert len(ups) == 1, "one leg of the day high; it re-arms only after a pullback"
    signals_before = len(s.store.signals)
    deliveries_before = len(s.store.deliveries)

    broken = ups[0].candle_start
    assert broken is not None
    replay = [row for row in s.store.candle_rows(1) if row.candle_start == broken]
    assert len(replay) == 1
    await s.pipeline.process_candles(replay, now=minute(25, second=4))
    await s.pipeline.process_candles(replay, now=minute(25, second=4))

    assert len(s.store.signals) == signals_before, (
        "the level-keyed episode id must make a restart replay a no-op"
    )
    assert len(s.store.deliveries) == deliveries_before
    assert s.pipeline.counters.breakouts_alerted == 1


async def test_a_disabled_strategy_pages_nobody():
    s = await build(
        [inst(1, "RELIANCE")],
        {1: rising_ticks(minutes=24)},
        simulation=False,
        telegram=True,
        strategies=session_params(session_break=False, orb=False, vwap=False),
    )
    await s.connect()
    await s.run_minutes(24)

    for kind in (SignalType.BREAKOUT_UP, SignalType.BREAKDOWN):
        assert s.signals_of(kind) == [], f"{kind.value} cannot appear with every strategy off"
    assert s.pipeline.breakouts.events_raised == 0
    assert len(s.store.candle_rows(1)) == 24, "the tape itself is unaffected by the switch"


async def test_telegram_off_for_one_strategy_keeps_the_board_row_and_drops_the_push():
    rise = inst(1, "RELIANCE")
    plan = {1: rising_ticks(minutes=24)}
    armed = await build(
        [rise], plan, simulation=False, telegram=True, strategies=session_params(**DAY_BREAK)
    )
    await armed.connect()
    await armed.run_minutes(24)
    pushed = {
        d.signal_id for d in armed.store.deliveries.values() if d.channel is Channel.TELEGRAM
    }
    assert pushed, "the same tape with the switch on does page the phone"

    quiet = await build(
        [rise],
        plan,
        simulation=False,
        telegram=True,
        strategies=session_params(**DAY_BREAK_BOARD_ONLY),
    )
    await quiet.connect()
    await quiet.run_minutes(24)

    ups = quiet.signals_of(SignalType.BREAKOUT_UP, instrument_id=1)
    assert len(ups) == 1, "the break still happened and the board still shows it"
    assert quiet.pipeline.counters.breakouts_alerted == 1
    for signal in ups:
        channels = {
            d.channel for d in quiet.store.deliveries.values() if d.signal_id == signal.id
        }
        assert channels == {Channel.DASHBOARD}, "telegram=False: recorded, never paged"
        assert signal.message is not None, "and the board row carries the same one-line why"


async def test_an_outage_suspends_level_breaks_while_the_candles_keep_storing():
    s = await build(
        [inst(1, "RELIANCE")],
        {1: rising_ticks(minutes=24)},
        strategies=session_params(**DAY_BREAK),
    )
    await s.connect()
    await s.run_minutes(10)
    before = len(s.signals_of(SignalType.BREAKOUT_UP))
    assert before == 1, "the break of candle 5 is the one alert this tape mints"

    await s.stale("test frames stopped", minute(10, second=30))
    await s.run_minutes(14)

    assert len(s.signals_of(SignalType.BREAKOUT_UP)) == before, (
        "a feed we cannot trust may not mint a level break (§7.2)"
    )
    assert len(s.store.candle_rows(1)) == 24, "while the candles are still stored"
    assert s.pipeline.counters.breakouts_detected == before, (
        "the detector never saw the outage minutes"
    )


async def test_the_orb_retest_watch_rides_the_pipeline_and_stays_silent():
    """The setup watch, end to end: the ORB break opens a leg, the reclaim closes it.

    Phase 2C observes only — the reclaim moves ``retests_detected`` and mints
    nothing; the ORB break itself remains the one alert on this tape.
    """
    s = await build(
        [inst(1, "RELIANCE")],
        {1: orb_retest_ticks()},
        strategies=session_params(
            orb={"min_volume_ratio": 0.0}, session_break=False, vwap=False, rsi=False
        ),
    )
    await s.connect()
    await s.run_minutes(11)  # through minute 10: the break is judged when bucket 2 opens

    legs = s.pipeline.setups.legs_for(1)
    assert len(legs) == 1, "the ORB break must open exactly one retest leg"
    assert legs[0].level == 100.2, "the leg watches the opening-range high"
    assert legs[0].break_candle_start == minute(9), "stamped with bucket 1's closing candle"
    assert legs[0].bars_since_break == 0
    assert s.pipeline.counters.retests_detected == 0
    ups = s.signals_of(SignalType.BREAKOUT_UP, instrument_id=1)
    assert len(ups) == 1, "the break is the one level-break alert on this tape"

    await s.run_minutes(2)  # minutes 11 and 12: the touch, then the reclaim

    assert s.pipeline.counters.retests_detected == 1
    assert s.pipeline.setups.open_leg_count == 0, "one leg, one candidate: consumed"
    assert len(s.signals_of(SignalType.BREAKOUT_UP, instrument_id=1)) == 1, (
        "the reclaim is observed, never alerted — no second level-break row"
    )


async def test_a_mid_day_restart_rebuilds_levels_without_alerting_for_the_morning():
    """The 11:40 restart: today's minutes are in the database, the tracker is empty.

    Recovery folds the stored tape into the levels *silently* — those breaks were
    judged when they happened, and paging the phone again for 09:20 at noon is spam.
    What it must rebuild is the real level: the day high the morning printed, and an
    opening range the restarted process never saw live. The range is the marquee
    result — without recovery the ORB detector is mute for the rest of the day.
    """
    rise = inst(1, "RELIANCE")
    plan = {1: rising_ticks(minutes=24)}
    strategies = session_params(
        session_break={"min_volume_ratio": 0.0},
        orb={"min_volume_ratio": 0.0},
        vwap=False,
    )
    s = await build([rise], plan, strategies=strategies)
    await s.connect()
    await s.run_minutes(24)
    day_high = round(max(candle.high for candle in s.store.candle_rows(1)), 4)
    or_high = round(max(candle.high for candle in s.store.candle_rows(1)[:5]), 4)
    assert s.signals_of(SignalType.BREAKOUT_UP, instrument_id=1), "the first process did alert"

    fresh = await build([rise], plan, store=s.store, strategies=strategies)
    signals_before = len(s.store.signals)
    deliveries_before = len(s.store.deliveries)

    summary = await fresh.pipeline.recover_breakouts()
    assert summary["candles_folded"] == 24, "every stored minute was replayed"
    assert summary["instruments_recovered"] == 1
    assert len(s.store.signals) == signals_before, "and not one new alert came out of it"
    assert len(s.store.deliveries) == deliveries_before
    assert fresh.pipeline.breakouts.events_raised == 0
    assert fresh.pipeline.counters.breakouts_alerted == 0

    state = fresh.pipeline.breakouts._state[1]  # noqa: SLF001 — this is the contract
    assert round(state.session.high, 4) == day_high, (
        "the day level is the morning's, not the level as of 11:40"
    )
    assert state.session.candles == 24
    assert state.orb.or_high is not None and round(state.orb.or_high, 4) == or_high, (
        "the 09:15-09:20 range was rebuilt from stored candles — without this the "
        "restarted process has no opening range and ORB is mute until tomorrow"
    )
    assert summary["levels"]["ranges_known"] == 1
