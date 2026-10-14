"""Unit tests for the breakout / breakdown detectors (worker.analytics.breakouts).

Every expected number is worked out by hand in the comment above it, from the
definition stated in the module docstring — never copied from the implementation's
own output — so a formula change has to be argued with arithmetic.

Candle starts come from ``tests.conftest.minute(n)``, so ``offset 0`` is the 09:15
bar and ``offset 5`` is the first bar of ORB bucket 1: the same clock
``ORB_STRATEGY_SQL`` uses on the dashboard.
"""

from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import BASE_DAY, minute
from worker.analytics.breakouts import (
    DAY_BREAK_MIN_CANDLES,
    DEFAULTS,
    DOWN,
    STRATEGIES,
    UP,
    BreakoutEvent,
    BreakoutTracker,
    StrategiesConfig,
    StrategyParams,
    parse_strategies,
    strategy_reason,
    unknown_strategies_in,
)
from worker.timeutils import SessionWindow, ist_zoneinfo


def window(day: date = BASE_DAY) -> SessionWindow:
    zone = ist_zoneinfo()
    return SessionWindow(
        trading_date=day,
        open_at=datetime.combine(day, time(9, 15), tzinfo=zone),
        close_at=datetime.combine(day, time(15, 30), tzinfo=zone),
        pre_open_start_at=datetime.combine(day, time(9, 0), tzinfo=zone),
        post_close_end_at=datetime.combine(day, time(16, 0), tzinfo=zone),
    )


def bar(
    offset: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: int = 1000,
    instrument_id: int = 1,
    day: date = BASE_DAY,
) -> SimpleNamespace:
    """One completed 1-minute bar, ``offset`` minutes after the 09:15 open."""
    start = minute(offset).replace(year=day.year, month=day.month, day=day.day)
    return SimpleNamespace(
        instrument_id=instrument_id,
        trading_date=day,
        candle_start=start,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=volume,
    )


def only(name: str, **tweaks: Any) -> StrategiesConfig:
    """One strategy armed (its defaults plus ``tweaks``), the other two off.

    So each test sees only its own rules firing and a stray alert from another
    strategy cannot be mistaken for the behaviour under test.
    """
    off = StrategyParams(
        enabled=False,
        threshold_pct=0.0,
        confirm_candles=1,
        min_volume_ratio=0.0,
        cooldown_minutes=0,
        telegram=False,
    )
    params = {
        key: off
        if key != name
        else StrategyParams(**{**DEFAULTS[key].to_dict(), **tweaks})
        for key in STRATEGIES
    }
    return StrategiesConfig(**params)  # type: ignore[arg-type]


def armed(name: str, **tweaks: Any) -> BreakoutTracker:
    return BreakoutTracker(strategies=only(name, **tweaks), window=window())


def day_break_t(**tweaks: Any) -> BreakoutTracker:
    """Day-break tracker with the volume gate open, so level logic is what is tested."""
    tweaks.setdefault("min_volume_ratio", 0.0)
    return armed("session_break", **tweaks)


def orb_t(**tweaks: Any) -> BreakoutTracker:
    tweaks.setdefault("min_volume_ratio", 0.0)
    tweaks.setdefault("threshold_pct", 0.0)
    return armed("orb", **tweaks)


def vwap_t(**tweaks: Any) -> BreakoutTracker:
    """Cross-logic tests armed with a one-candle confirmation.

    The shipped default is two confirmations (a single minute through VWAP is the
    noisiest signal on the tape); pinning it to one here keeps each test below
    judging *the cross* rather than the counter, and
    ``test_vwap_confirm_candles_count_consecutive_closes_beyond_the_band`` and
    the defaults test cover the counting.
    """
    tweaks.setdefault("min_volume_ratio", 0.0)
    tweaks.setdefault("threshold_pct", 0.10)
    tweaks.setdefault("confirm_candles", 1)
    return armed("vwap", **tweaks)


def feed(t: BreakoutTracker, candles: list[SimpleNamespace], **kwargs: Any) -> list[BreakoutEvent]:
    out: list[BreakoutEvent] = []
    for candle in candles:
        out += t.on_candle(candle, symbol="TCS", **kwargs)
    return out


def five_flat() -> list[SimpleNamespace]:
    """Five bars at ₹100 flat: a day high and day low of exactly 100.00."""
    return [bar(o, 100.0) for o in range(DAY_BREAK_MIN_CANDLES)]


# ── parse_strategies ────────────────────────────────────────────────────────


def test_missing_document_is_the_defaults():
    assert parse_strategies(None) == StrategiesConfig()
    assert parse_strategies({}) == StrategiesConfig()
    assert parse_strategies({"strategies": {}}) == StrategiesConfig()
    assert parse_strategies(None).orb == DEFAULTS["orb"]
    assert parse_strategies(None).any_enabled is True


def test_partial_document_keeps_the_other_defaults():
    parsed = parse_strategies({"strategies": {"orb": {"enabled": False}}})
    assert parsed.orb.enabled is False
    assert parsed.orb.threshold_pct == DEFAULTS["orb"].threshold_pct
    assert parsed.vwap == DEFAULTS["vwap"]


def test_both_shapes_accepted_and_strings_coerced():
    bare = parse_strategies(
        {"vwap": {"threshold_pct": "0.25", "confirm_candles": "3", "telegram": "no"}}
    )
    assert bare.vwap.threshold_pct == 0.25
    assert bare.vwap.confirm_candles == 3
    assert bare.vwap.telegram is False
    assert bare.session_break == DEFAULTS["session_break"]


def test_invalid_values_raise_naming_the_strategy_and_field():
    with pytest.raises(ValueError, match="orb: threshold_pct"):
        parse_strategies({"strategies": {"orb": {"threshold_pct": 40}}})
    with pytest.raises(ValueError, match="vwap: confirm_candles"):
        parse_strategies({"strategies": {"vwap": {"confirm_candles": 0}}})
    with pytest.raises(ValueError, match="session_break: cooldown_minutes"):
        parse_strategies({"strategies": {"session_break": {"cooldown_minutes": 900}}})
    with pytest.raises(ValueError, match="expected a boolean"):
        parse_strategies({"strategies": {"orb": {"enabled": "maybe"}}})
    with pytest.raises(ValueError, match="JSON object"):
        parse_strategies(["orb"])
    with pytest.raises(ValueError, match="orb: expected an object"):
        parse_strategies({"strategies": {"orb": True}})


def test_unknown_strategy_is_ignored_not_fatal():
    # A newer dashboard must never take the tape down by offering a strategy this
    # worker build cannot run; it is reported separately so the log can say so.
    doc = {"strategies": {"orb": {"enabled": True}, "rsi_cross": {"enabled": True}}}
    assert parse_strategies(doc).orb.enabled is True
    assert unknown_strategies_in(doc) == ["rsi_cross"]
    assert unknown_strategies_in(None) == []


def test_document_round_trips_through_the_stored_shape():
    original = StrategiesConfig(
        session_break=StrategyParams(threshold_pct=1.25, confirm_candles=4),
        orb=DEFAULTS["orb"],
        vwap=DEFAULTS["vwap"],
    )
    assert parse_strategies(original.to_dict()) == original
    assert set(original.to_dict()["strategies"]) == set(STRATEGIES)
    assert set(original.summary()) == set(STRATEGIES)
    assert original.get("orb") is original.orb
    with pytest.raises(KeyError):
        original.get("rsi_cross")


# ── session high / low ──────────────────────────────────────────────────────


def test_day_break_needs_five_minutes_of_tape_first():
    t = day_break_t()
    # Four candles of history: the fifth is not evaluable yet…
    assert feed(t, [bar(o, 100.0) for o in range(4)]) == []
    assert t.on_candle(bar(4, 101.0), symbol="TCS") == []
    # …but with five candles behind it, closing above the ₹101 high is a break.
    fired = t.on_candle(bar(5, 102.0), symbol="TCS")
    assert len(fired) == 1
    assert (fired[0].strategy, fired[0].direction) == ("session_break", UP)
    assert fired[0].level == pytest.approx(101.0)
    assert fired[0].price == pytest.approx(102.0)
    assert fired[0].symbol == "TCS"


def test_day_break_trigger_is_the_threshold_not_the_high_itself():
    # Threshold 0.10% → the mark is 100 × 1.001 = 100.10. A close of 100.05 is a
    # new session high but not a break; 100.15 is.
    t = day_break_t()
    feed(t, five_flat())
    assert feed(t, [bar(5, 100.05)]) == []
    events = feed(t, [bar(6, 100.15)])
    assert len(events) == 1
    assert events[0].exceed_pct == pytest.approx(0.15, abs=1e-9)


def test_day_break_alerts_once_per_leg_and_rearms_on_a_pullback():
    t = day_break_t()
    feed(t, five_flat())
    first = feed(t, [bar(5, 101.0)])
    assert len(first) == 1 and first[0].level == pytest.approx(100.0)
    # Price keeps running: each later close is a new day high and none of them is
    # news, because the strategy is disarmed until the broken level is again a
    # level price is under.
    assert feed(t, [bar(6, 102.0), bar(7, 103.0), bar(8, 104.0)]) == []
    # Dropping under ₹100 breaks the day *low* (the flat open is still the low)
    # and re-arms the high side against ₹104.
    pullback = feed(t, [bar(9, 99.0)])
    assert [e.direction for e in pullback] == [DOWN]
    assert pullback[0].level == pytest.approx(100.0)
    assert feed(t, [bar(10, 104.05)]) == []  # 104 × 1.001 = 104.104, not reached
    second = feed(t, [bar(11, 104.20)])
    assert len(second) == 1
    assert second[0].level == pytest.approx(104.0)
    assert second[0].episode_id != first[0].episode_id


def test_day_low_break_is_the_mirror_image():
    t = day_break_t()
    feed(t, five_flat())
    events = feed(t, [bar(5, 99.85)])  # mark = 100 × 0.999 = 99.90
    assert len(events) == 1
    assert events[0].direction == DOWN
    assert events[0].level == pytest.approx(100.0)
    assert "day_low_break" in events[0].reasons
    # The low is now ₹99.85 and the strategy is disarmed on that side; a close
    # back at ₹99.95 breaks nothing.
    assert feed(t, [bar(6, 99.95)]) == []


def test_volume_gate_blocks_a_thin_break_and_zero_turns_it_off():
    t = armed("session_break", min_volume_ratio=1.0)
    feed(t, five_flat(), relative_volume=2.0)
    assert t.on_candle(bar(5, 101.0), symbol="X", relative_volume=0.5) == []
    # An unknown ratio fails the gate too: no evidence, no alert.
    assert t.on_candle(bar(6, 101.0), symbol="X", relative_volume=None) == []
    assert len(t.on_candle(bar(7, 101.0), symbol="X", relative_volume=1.0)) == 1
    loose = day_break_t()  # min_volume_ratio 0 → gate off
    feed(loose, five_flat())
    assert len(loose.on_candle(bar(5, 101.0), symbol="X", relative_volume=None)) == 1


def test_confirm_candles_requires_consecutive_closes_beyond_the_level():
    t = day_break_t(confirm_candles=2)
    feed(t, five_flat())
    assert feed(t, [bar(5, 101.0)]) == []  # 1 of 2 beyond the mark
    # A close back at the level restarts the count…
    assert feed(t, [bar(6, 100.00)]) == []
    assert feed(t, [bar(7, 101.0)]) == []  # 1 of 2 again
    fired = feed(t, [bar(8, 101.20)])
    assert len(fired) == 1
    assert "confirmed_2_candles" in fired[0].reasons


def test_disabled_strategy_emits_nothing():
    t = vwap_t()  # session_break is off in this config
    events = feed(t, five_flat(), relative_volume=2.0) + feed(
        t, [bar(5, 140.0)], relative_volume=2.0
    )
    assert events == []
    assert t.events_raised == 0


# ── opening range ───────────────────────────────────────────────────────────

RANGE = {"close": 100.0, "high": 102.0, "low": 98.0}  # opening range: 98.00–102.00


def play(
    t: BreakoutTracker, buckets: list[dict[str, Any]], *, symbol: str = "AXIS"
) -> list[list[BreakoutEvent]]:
    """Feed 5-minute buckets in order; return the events each bucket produced.

    Element ``i`` is what the tracker emitted while processing bucket ``i``, which
    is bucket ``i-1``'s verdict — a bucket is only final once the next one starts.
    The trailing element is the end-of-day ``flush()``, which judges the last
    bucket. That one-minute lag is the point of the shape.
    """
    verdicts: list[list[BreakoutEvent]] = []
    for k, spec in enumerate(buckets):
        emitted: list[BreakoutEvent] = []
        closes = spec["close"] if isinstance(spec["close"], list) else [spec["close"]] * 5
        volumes = spec["volume"] if isinstance(spec.get("volume"), list) else [spec.get("volume", 1000)] * 5
        for o in range(5):
            emitted += t.on_candle(
                bar(5 * k + o, closes[o], high=spec.get("high"), low=spec.get("low"), volume=volumes[o]),
                symbol=symbol,
            )
        verdicts.append(emitted)
    verdicts.append(t.flush())
    return verdicts


def test_bucket_index_matches_the_boards_sql():
    t = orb_t()
    assert t.bucket_of(minute(0)) == 0
    assert t.bucket_of(minute(4)) == 0
    assert t.bucket_of(minute(5)) == 1
    assert t.bucket_of(minute(74)) == 14
    # Outside 09:15–15:30 there is no bucket, exactly as the SQL's time filter cuts.
    assert t.bucket_of(minute(375)) is None
    assert t.bucket_of(minute(0, second=-1)) is None


def test_bucket_zero_sets_the_range_and_can_never_fire():
    t = orb_t()
    verdicts = play(t, [RANGE])
    assert verdicts == [[], []]  # opened, then judged by the flush: the range is set
    orb = t._state[1].orb
    assert orb.or_high == pytest.approx(102.0)
    assert orb.or_low == pytest.approx(98.0)
    assert orb.fired_up is False


def test_opening_range_uses_every_bar_of_the_first_five_minutes():
    t = orb_t()
    play(
        t,
        [
            {"close": [100.0, 100.5, 101.0, 100.2, 99.8], "high": 102.0, "low": 98.0},
            {"close": 100.0, "high": 100.0, "low": 100.0},
        ],
    )
    orb = t._state[1].orb
    assert orb.or_high == pytest.approx(102.0)  # max(high), not the last close
    assert orb.or_low == pytest.approx(98.0)  # min(low)
    assert orb.or_high > orb.or_low


def test_orb_fires_on_the_bucket_close_that_crosses_the_range():
    t = orb_t()
    verdicts = play(
        t,
        [
            RANGE,
            # Bucket 1 = 09:20–09:24, closing higher each minute; its close is the
            # 09:24 bar at 102.50, above the range high of 102.00.
            {"close": [100.5, 101.0, 101.5, 102.0, 102.5], "high": 103.0, "low": 100.0},
            {"close": 102.0, "high": 102.0, "low": 102.0},
        ],
    )
    assert verdicts[0] == [] and verdicts[1] == []
    fired = verdicts[2]  # judged when bucket 2's first bar arrives: one minute late
    assert len(fired) == 1
    event = fired[0]
    assert (event.strategy, event.direction) == ("orb", UP)
    assert event.level == pytest.approx(102.0)
    assert event.price == pytest.approx(102.5)  # the *bucket* close, not 09:25's 102.0
    assert event.candle_start == minute(9)  # 09:24, the minute the break happened
    assert "orb_high_break" in event.reasons


def test_orb_volume_ratio_is_bucket_volume_over_the_mean_of_all_prior_buckets():
    # Range bucket: 5 × 1000 = 5000. Breakout bucket: 5 × 2000 = 10000.
    # Prior mean = 5000 / 1 bucket = 5000 → ratio 10000 / 5000 = 2.0, which is the
    # SQL's `volume / (volume_before / buckets_before)` with bucket 0 included.
    buckets = [
        {**RANGE, "volume": 1000},
        {"close": 103.0, "high": 103.0, "low": 100.0, "volume": 2000},
        {"close": 103.0, "high": 103.0, "low": 103.0, "volume": 500},
    ]
    t = armed("orb", min_volume_ratio=2.0, threshold_pct=0.0)  # gate exactly at 2.0
    verdicts = play(t, buckets)
    assert len(verdicts[2]) == 1
    assert verdicts[2][0].volume_ratio == pytest.approx(2.0)
    assert "volume_2.00x" in verdicts[2][0].reasons
    # One notch under the gate and the same tape reports nothing.
    strict = armed("orb", min_volume_ratio=2.01, threshold_pct=0.0)
    assert play(strict, buckets)[2] == []


def test_orb_gate_blocks_a_low_volume_break_without_consuming_it():
    t = armed("orb", min_volume_ratio=3.0, threshold_pct=0.0)
    verdicts = play(
        t,
        [
            {**RANGE, "volume": 1000},
            # Bucket 1 closes beyond the range on 2.0× — under the 3.0× gate → nothing.
            {"close": 103.0, "high": 103.0, "low": 100.0, "volume": 2000},
            # Bucket 2: 5 × 9000 = 45000 over a prior mean of (5000 + 10000) / 2 =
            # 7500 → 6.0× ≥ 3.0 → the break is reported, at this bucket's close.
            {"close": 104.0, "high": 104.0, "low": 100.0, "volume": 9000},
        ],
    )
    assert verdicts[2] == []
    assert len(verdicts[3]) == 1
    assert verdicts[3][0].price == pytest.approx(104.0)
    assert verdicts[3][0].volume_ratio == pytest.approx(6.0)


def test_orb_fires_once_per_direction_per_session():
    t = orb_t()
    verdicts = play(
        t,
        [
            RANGE,
            {"close": 103.0, "high": 103.0, "low": 103.0},
            {"close": 105.0, "high": 105.0, "low": 105.0},
            {"close": 106.0, "high": 106.0, "low": 106.0},
        ],
    )
    assert sum(len(v) for v in verdicts) == 1
    assert t.by_strategy["orb"] == 1
    assert t._state[1].orb.fired_up is True


def test_orb_breakdown_uses_the_range_low():
    t = orb_t()
    verdicts = play(
        t,
        [
            RANGE,
            {"close": 97.0, "high": 98.0, "low": 97.0},
            {"close": 96.0, "high": 96.0, "low": 96.0},
        ],
    )
    assert [(e.strategy, e.direction, round(e.level, 2)) for e in verdicts[2]] == [
        ("orb", DOWN, 98.0)
    ]
    assert "orb_low_break" in verdicts[2][0].reasons


def test_orb_confirmation_counts_buckets_and_resets_inside_the_range():
    t = orb_t(confirm_candles=2)
    verdicts = play(
        t,
        [
            RANGE,
            {"close": 103.0, "high": 103.0, "low": 103.0},  # beyond: 1 of 2
            {"close": 103.0, "high": 103.0, "low": 103.0},  # beyond: 2 of 2 → fires
        ],
    )
    assert verdicts[2] == []  # bucket 1's verdict: not enough yet
    assert len(verdicts[3]) == 1  # bucket 2's verdict, delivered by the flush

    reset = orb_t(confirm_candles=2)
    verdicts = play(
        reset,
        [
            RANGE,
            {"close": 103.0, "high": 103.0, "low": 103.0},  # 1 of 2
            {"close": 100.0, "high": 100.0, "low": 100.0},  # back inside → count resets
            {"close": 103.0, "high": 103.0, "low": 103.0},  # 1 of 2 again
            {"close": 103.0, "high": 103.0, "low": 103.0},  # 2 of 2 → fires
        ],
    )
    assert verdicts[2] == [] and verdicts[3] == [] and verdicts[4] == []
    assert len(verdicts[5]) == 1


def test_flush_judges_the_last_bucket_of_the_day():
    t = orb_t()
    for o in range(5):
        assert t.on_candle(bar(o, 100.0, high=102.0, low=98.0), symbol="ITC") == []
    # The 15:25–15:29 bucket closes above the range and nothing follows it, so only
    # flush() can report it — which is what makes the live feed agree with the
    # board's after-the-fact query.
    for o in range(5):
        assert t.on_candle(bar(370 + o, 103.0, high=103.0, low=103.0), symbol="ITC") == []
    events = t.flush()
    assert len(events) == 1
    assert events[0].candle_start == minute(374)
    assert t.flush() == []  # idempotent: no open bucket is left


# ── VWAP ────────────────────────────────────────────────────────────────────


def feed_vwap(
    t: BreakoutTracker,
    closes: list[float],
    *,
    vwap: float = 100.0,
    rv: float | None = None,
    start: int = 0,
) -> list[BreakoutEvent]:
    out: list[BreakoutEvent] = []
    for i, close in enumerate(closes):
        out += t.on_candle(
            bar(start + i, close), symbol="WIPRO", vwap=vwap, relative_volume=rv
        )
    return out


def test_first_side_of_the_day_is_a_baseline_not_a_reclaim():
    t = vwap_t()
    assert feed_vwap(t, [100.5]) == []  # above, adopted silently
    assert t._state[1].vwap.side == UP
    other = vwap_t()
    assert feed_vwap(other, [99.0]) == []  # below, likewise
    assert other._state[1].vwap.side == DOWN


def test_vwap_flips_alert_in_both_directions_with_the_vwap_as_level():
    t = vwap_t()
    feed_vwap(t, [100.5])  # baseline ABOVE
    loss = feed_vwap(t, [99.0], start=1)
    assert [(e.direction, round(e.level, 2)) for e in loss] == [(DOWN, 100.0)]
    assert "vwap_loss" in loss[0].reasons
    reclaim = feed_vwap(t, [100.5], start=2)
    assert [e.direction for e in reclaim] == [UP]
    assert "vwap_reclaim" in reclaim[0].reasons
    assert reclaim[0].episode_id != loss[0].episode_id
    # The level is the VWAP *of that moment*, so a later cross cannot collide:
    later = feed_vwap(t, [99.0], vwap=99.6, start=3)
    assert later[0].level == pytest.approx(99.6)


def test_staying_on_one_side_never_repeats():
    t = vwap_t()
    feed_vwap(t, [100.5])
    assert feed_vwap(t, [100.6, 100.7, 100.8], start=1) == []
    assert t.events_raised == 0


def test_hysteresis_band_stops_a_hugging_tape_from_flapping():
    t = vwap_t()
    feed_vwap(t, [100.5])  # ABOVE
    # Band 0.10% → a loss needs a close under 99.90.
    assert feed_vwap(t, [99.95, 99.92], start=1) == []
    assert len(feed_vwap(t, [99.85], start=3)) == 1


def test_vwap_confirm_candles_count_consecutive_closes_beyond_the_band():
    t = vwap_t(confirm_candles=2)
    feed_vwap(t, [100.5, 100.6, 100.7])  # the 2nd candle adopts ABOVE (silently)
    assert feed_vwap(t, [99.0], start=3) == []  # 1 of 2
    assert feed_vwap(t, [100.0], start=4) == []  # inside the band: count held
    assert len(feed_vwap(t, [99.4], start=5)) == 1


def test_vwap_volume_gate_blocks_a_weak_cross_without_losing_it():
    t = vwap_t(min_volume_ratio=1.5)
    feed_vwap(t, [100.5], rv=2.0)  # baseline ABOVE
    assert feed_vwap(t, [99.0], rv=1.0, start=1) == []  # thin loss: suppressed
    # The standing side was kept, so the same move on real volume still crosses:
    assert len(feed_vwap(t, [99.1], rv=2.0, start=2)) == 1
    assert feed_vwap(t, [99.2], rv=2.0, start=3) == []


def test_vwap_needs_a_vwap():
    t = vwap_t()
    assert t.on_candle(bar(0, 100.0), symbol="X", vwap=None, relative_volume=2.0) == []


# ── tracker behaviour ───────────────────────────────────────────────────────


def test_candles_of_another_session_are_ignored_entirely():
    t = day_break_t()
    yesterday = date(2026, 10, 9)
    events = feed(t, [bar(o, 100.0 + o * 5, day=yesterday) for o in range(8)])
    assert events == []
    assert t._state == {}  # not even a level was recorded from foreign tape
    # Today's first candle therefore starts from nothing, not from ₹135.
    assert feed(t, [bar(0, 100.0)]) == []
    assert t._state[1].session.high == pytest.approx(100.0)


def test_reconfigure_keeps_levels_and_applies_new_thresholds():
    t = day_break_t()
    feed(t, five_flat())
    t.reconfigure(only("session_break", threshold_pct=5.0, min_volume_ratio=0.0))
    assert feed(t, [bar(5, 101.0)]) == []  # 1% is not 5%
    t.reconfigure(only("session_break", threshold_pct=0.10, min_volume_ratio=0.0))
    assert len(feed(t, [bar(6, 101.0)])) == 1


def test_instruments_are_tracked_independently():
    t = day_break_t()
    for o in range(5):
        t.on_candle(bar(o, 100.0, instrument_id=7), symbol="A")
        t.on_candle(bar(o, 200.0, instrument_id=9), symbol="B")
    assert len(t.on_candle(bar(5, 100.2, instrument_id=7), symbol="A")) == 1
    assert len(t.on_candle(bar(5, 200.4, instrument_id=9), symbol="B")) == 1
    assert t.armed_summary()["tracked_instruments"] == 2


def test_events_are_level_keyed_and_deterministic():
    first = day_break_t()
    feed(first, five_flat())
    events = feed(first, [bar(5, 100.20)])
    assert len(events) == 1
    second = day_break_t()  # a fresh process, same tape
    feed(second, five_flat())
    again = feed(second, [bar(5, 100.20)])
    # Same tape, second process: the same idempotency identity, so the database's
    # UNIQUE constraint — not the worker's memory — is what stops a double alert.
    assert again[0].episode_id == events[0].episode_id
    assert again[0].episode_id == f"{BASE_DAY.isoformat()}-1-session_break-UP-100.0000"
    assert events[0].reasons[0] == "strategy:session_break"
    assert strategy_reason("session_break") in events[0].reasons


def test_event_carries_the_alert_layers_knobs():
    t = armed("session_break", cooldown_minutes=7, telegram=False, min_volume_ratio=0.0)
    feed(t, five_flat())
    event = feed(t, [bar(5, 101.0)])[0]
    assert (event.cooldown_minutes, event.telegram) == (7, False)
    assert event.trading_date == BASE_DAY
    assert event.to_log_dict()["symbol"] == "TCS"


def test_event_describe_reads_like_an_alert():
    event = BreakoutEvent(
        strategy="orb",
        direction=UP,
        instrument_id=1,
        symbol="TATAMOTORS",
        level=102.0,
        price=103.02,
        candle_start=minute(9),
        trading_date=BASE_DAY,
        volume_ratio=2.5,
        cooldown_minutes=0,
        telegram=True,
        reasons=(strategy_reason("orb"), "orb_high_break"),
    )
    assert event.describe() == (
        "TATAMOTORS broke above the opening range ₹102.00 at ₹103.02 (+1.00%) "
        "on 2.50x volume"
    )


def test_counters_and_summary_report_what_fired():
    t = day_break_t()
    feed(t, five_flat())
    feed(t, [bar(5, 101.0)])
    assert t.events_raised == 1
    assert t.by_strategy == {"session_break": 1, "orb": 0, "vwap": 0}
    summary = t.armed_summary()
    assert summary["tracked_instruments"] == 1
    assert summary["ranges_known"] == 0
