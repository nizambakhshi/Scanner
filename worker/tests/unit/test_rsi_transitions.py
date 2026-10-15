"""RSI alerts on the reversed rule: one ping per bias *change*, never per session.

The operator's numbers did not move on 2026-10-15; their *meaning* did. Daily and
weekly RSI both **below 40** is the green side — ``ENTRY`` — and both **above 60**
is the red one — ``OVERBOUGHT``; the 40–60 band is NEUTRAL. ``_emit_rsi_transitions``
runs once at session start against ``stock_details`` and must hold the same four
line rules it always did: a first sighting baselines silently (rollout day cannot
page 212 times), a standing state stays quiet, only a transition into ENTRY or
OVERBOUGHT alerts, and a transition into NEUTRAL is recorded as the new baseline so
rotating back out of the band later reads as a real change.

What is new here is that these two types are shared with the intraday five-minute
detector, so the file also pins the one thing that makes the sharing safe: the
nightly pair asks for no cooldown window, so a morning page can never mute the
afternoon dip that follows it.
"""

from __future__ import annotations

from datetime import date, timedelta

from tests.conftest import BASE_DAY, SESSION_CLOSE, SESSION_OPEN
from worker.config import WorkerSettings
from worker.main import WorkerRuntime, _rsi_message
from worker.models import (
    Instrument,
    RsiState,
    Signal,
    SignalType,
    StockTechnicals,
)
from worker.persistence.memory import InMemoryStore
from worker.timeutils import load_calendar


def runtime(store: InMemoryStore) -> WorkerRuntime:
    settings = WorkerSettings(
        scan_mode="live",
        simulation_label=False,
        timezone="Asia/Kolkata",
        app_url="",
    )
    return WorkerRuntime(settings=settings, store=store, calendar=load_calendar())


def instrument(iid: int, symbol: str) -> Instrument:
    return Instrument(
        id=iid,
        exchange="NSE",
        symbol=symbol,
        trading_symbol=symbol,
        company_name=f"{symbol} Limited",
        instrument_token=str(1000 + iid),
    )


async def seed(
    store: InMemoryStore,
    instrument_id: int,
    *,
    bias: str | None,
    last: str | None = None,
    daily: float | None = 35.4,
    weekly: float | None = 37.1,
) -> None:
    await store.update_stock_technicals(
        [
            StockTechnicals(
                instrument_id=instrument_id,
                daily_rsi=daily,
                weekly_rsi=weekly,
                rsi_bias=bias,
                rsi_as_of=date(2026, 10, 10),
                data_start=date(2002, 1, 1),
                high_52w=1700.0,
                low_52w=1200.0,
                ath=1700.0,
                atl=10.0,
            )
        ]
    )
    if last is not None:
        await store.set_last_alert_bias(instrument_id, last)


def signals_of(store: InMemoryStore, kind: SignalType) -> list[Signal]:
    return [row for row in store.signals.values() if row.signal_type == kind]


async def emit(store: InMemoryStore, instruments: dict[int, Instrument]):
    rt = runtime(store)
    rt._build_alerts()
    session = await store.ensure_session(BASE_DAY, SESSION_OPEN, SESSION_CLOSE)
    await rt._emit_rsi_transitions(session, instruments)
    return session


async def test_first_sighting_baselines_silently() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="ENTRY", last=None)
    await emit(store, {1: instrument(1, "RELIANCE")})

    assert signals_of(store, SignalType.RSI_ENTRY) == []
    assert store.stock_details[1]["last_alert_bias"] == "ENTRY"


async def test_transition_into_entry_alerts_once_keyed_on_session() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="ENTRY", last="NEUTRAL")
    session = await emit(store, {1: instrument(1, "RELIANCE")})

    emitted = signals_of(store, SignalType.RSI_ENTRY)
    assert len(emitted) == 1
    signal = emitted[0]
    assert signal.idempotency_key == f"RSI_ENTRY:1:{session.id}"
    assert signal.symbol == "RELIANCE"
    assert "below 40" in (signal.message or "")
    assert signal.reasons == ("daily rsi below 40", "weekly rsi below 40")
    assert store.stock_details[1]["last_alert_bias"] == "ENTRY"
    deliveries = [row for row in store.deliveries.values() if row.signal_id == signal.id]
    assert [row.channel.value for row in deliveries] == ["DASHBOARD"]

    # A second start inside the same session sees a standing state: quiet.
    await emit(store, {1: instrument(1, "RELIANCE")})
    assert len(signals_of(store, SignalType.RSI_ENTRY)) == 1


async def test_transition_into_overbought_is_the_red_side() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="OVERBOUGHT", last="NEUTRAL", daily=63.2, weekly=61.8)
    await emit(store, {1: instrument(1, "RELIANCE")})

    emitted = signals_of(store, SignalType.RSI_OVERBOUGHT)
    assert len(emitted) == 1
    assert signals_of(store, SignalType.RSI_ENTRY) == []
    assert "above 60" in (emitted[0].message or "")
    assert emitted[0].reasons == ("daily rsi above 60", "weekly rsi above 60")
    # Nothing raises the retired names any more — that is the whole rename.
    assert signals_of(store, SignalType.RSI_BULLISH) == []
    assert signals_of(store, SignalType.RSI_BEARISH) == []


async def test_restart_between_alert_and_bias_write_cannot_alert_twice() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="ENTRY", last="NEUTRAL")
    await emit(store, {1: instrument(1, "RELIANCE")})
    # Simulate the crash window: the signal row survived, the bias write did not.
    store.stock_details[1]["last_alert_bias"] = "NEUTRAL"

    await emit(store, {1: instrument(1, "RELIANCE")})

    assert len(signals_of(store, SignalType.RSI_ENTRY)) == 1
    assert store.stock_details[1]["last_alert_bias"] == "ENTRY"


async def test_neutral_transition_is_quiet_and_becomes_the_new_baseline() -> None:
    store = InMemoryStore()
    await seed(store, 1, bias="NEUTRAL", last="ENTRY", daily=52.0, weekly=48.0)
    await emit(store, {1: instrument(1, "RELIANCE")})

    assert signals_of(store, SignalType.RSI_ENTRY) == []
    assert signals_of(store, SignalType.RSI_OVERBOUGHT) == []
    assert store.stock_details[1]["last_alert_bias"] == "NEUTRAL"

    # Rotating back out of the band is a real change from the recorded baseline.
    await seed(store, 1, bias="OVERBOUGHT", daily=63.2, weekly=61.8)
    await emit(store, {1: instrument(1, "RELIANCE")})

    emitted = signals_of(store, SignalType.RSI_OVERBOUGHT)
    assert len(emitted) == 1
    assert "above 60" in (emitted[0].message or "")
    assert emitted[0].reasons == ("daily rsi above 60", "weekly rsi above 60")


async def test_unevaluable_and_out_of_universe_rows_are_skipped() -> None:
    store = InMemoryStore()
    # No daily/weekly pair to judge — the sync could not classify it.
    await seed(store, 1, bias=None, last=None, daily=None, weekly=None)
    # Classified, but not part of today's subscribed universe.
    await seed(store, 99, bias="ENTRY", last="NEUTRAL")
    await emit(store, {1: instrument(1, "RELIANCE")})

    assert signals_of(store, SignalType.RSI_ENTRY) == []
    assert store.stock_details[1].get("last_alert_bias") is None
    assert store.stock_details[99]["last_alert_bias"] == "NEUTRAL"


async def test_the_two_rsi_horizons_do_not_mute_each_other() -> None:
    # Both horizons raise RSI_ENTRY. The intraday detector carries its own 30-minute
    # window, keyed on the ``strategy:rsi`` reason; the nightly pair asks for no
    # window at all, because its once-per-session idempotency key already dedupes it.
    # Neither is allowed to swallow the other's alert.
    store = InMemoryStore()
    session = await store.ensure_session(BASE_DAY, SESSION_OPEN, SESSION_CLOSE)
    worker = runtime(store)
    worker._build_alerts()
    engine = worker._alerts
    assert engine is not None

    await engine.raise_alert(
        signal_type=SignalType.RSI_ENTRY,
        instrument_id=1,
        session_id=session.id,
        event_timestamp=SESSION_OPEN,
        episode_id=f"{BASE_DAY.isoformat()}-1-rsi-DOWN-37.2000",
        candle_start=SESSION_OPEN,
        reasons=("strategy:rsi", "rsi_oversold"),
        cooldown_minutes=30,
    )
    # A second dip five minutes later is inside that window: the row is written for
    # the board, the phone stays quiet. That is the strategy's own cooldown working.
    again = await engine.raise_alert(
        signal_type=SignalType.RSI_ENTRY,
        instrument_id=1,
        session_id=session.id,
        event_timestamp=SESSION_OPEN + timedelta(minutes=5),
        episode_id=f"{BASE_DAY.isoformat()}-1-rsi-DOWN-36.4000",
        candle_start=SESSION_OPEN + timedelta(minutes=5),
        reasons=("strategy:rsi", "rsi_oversold"),
        cooldown_minutes=30,
    )
    assert again.suppressed_reason == "cooldown 30m"

    # The nightly pair raised afterwards must still go out: it never asks for a
    # window, so the detector's cannot reach it.
    await seed(store, 1, bias="ENTRY", last="NEUTRAL")
    await emit(store, {1: instrument(1, "RELIANCE")})
    nightly = [row for row in store.signals.values() if row.signal_type is SignalType.RSI_ENTRY]
    assert len(nightly) == 3  # two intraday rows (one suppressed) + this one
    assert len(signals_of(store, SignalType.RSI_ENTRY)) == 3


def test_rsi_message_formats_the_pair() -> None:
    oversold = RsiState(
        instrument_id=1,
        daily_rsi=35.44,
        weekly_rsi=37.12,
        rsi_bias="ENTRY",
        last_alert_bias="NEUTRAL",
    )
    assert _rsi_message(oversold) == "Daily RSI 35.4 · weekly RSI 37.1 — both below 40"
    expensive = RsiState(
        instrument_id=1,
        daily_rsi=63.25,
        weekly_rsi=61.84,
        rsi_bias="OVERBOUGHT",
        last_alert_bias="NEUTRAL",
    )
    assert _rsi_message(expensive) == "Daily RSI 63.2 · weekly RSI 61.8 — both above 60"
